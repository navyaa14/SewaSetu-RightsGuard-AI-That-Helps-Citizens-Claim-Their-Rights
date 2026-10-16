# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import secrets
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))


def setting(section, key, env=None, default=None):
    """Config value, overridable by environment (deployment secrets never live in git)."""
    if env and os.environ.get(env):
        return os.environ[env]
    if config.has_option(section, key):
        return config.get(section, key)
    return default


app = Flask(__name__)
# The secret signs session cookies (including the departmental `admin` flag). The value that
# shipped in app.ini is public, so anyone could forge an officer session with it: refuse it.
_PUBLISHED_KEY = config.get("app", "secret_key", fallback="")
_secret = os.environ.get("SECRET_KEY", "")
if not _secret or _secret == _PUBLISHED_KEY:
    if os.environ.get("SEWASETU_DEV") == "1":
        _secret = "dev-only-" + hashlib.sha256(b"sewasetu-dev").hexdigest()
    else:
        raise RuntimeError("Set SECRET_KEY to a long random value (the one in app.ini is public).")
app.secret_key = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = setting("app", "sms_gateway_url", "SMS_GATEWAY_URL")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = setting("app", "upload_dir", "UPLOAD_DIR")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

# The AI-assistant channel is piloted in a subset of blocks (Director's note). Configurable,
# so the pilot can grow without a code change. Applicants elsewhere are pointed to the web
# form or the Block Development Office; the scheme rules themselves are the same everywhere.
AGENT_BLOCKS = [b.strip() for b in
                (setting("agent", "pilot_blocks", "AGENT_BLOCKS", ",".join(BLOCKS)) or "").split(",")
                if b.strip() in BLOCKS]

# Rejection reasons, as already recorded by block officers in the audit log.
REJECTION_REASONS = {
    "BANK_NOT_IN_NAME": "bank account not in applicant's name",
    "DUPLICATE_BENEFICIARY": "duplicate of an earlier beneficiary record",
    "AGE_PROOF_ILLEGIBLE": "age proof document illegible",
    "AGE_PROOF_MISMATCH": "age proof does not match stated date of birth",
    "UNDER_AGE": "applicant below 60 years of age",
    "OTHER": "other (see note)",
}

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=setting("database", "host", "DB_HOST"),
        port=setting("database", "port", "DB_PORT"),
        dbname=setting("database", "name", "DB_NAME"),
        user=setting("database", "user", "DB_USER"),
        password=setting("database", "password", "DB_PASSWORD"),
    )


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, datetime.now()))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def canonical_block(raw):
    """'sonari ' -> 'Sonari'. Unknown names are returned as typed so validation reports them."""
    raw = (raw or "").strip()
    for b in BLOCKS:
        if raw.lower() == b.lower():
            return b
    return raw[:50]


def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": sanitize(data.get("village")),
        "block": canonical_block(data.get("block")),
        "gender": sanitize(data.get("gender"), 10),
        "marital_status": sanitize(data.get("marital_status"), 20),
        "husband_name": sanitize(data.get("husband_name")),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or "")[:30],
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper()[:15],
        "doc_path": doc_path or "",
        "dob": None,
    }
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    if cleaned["bank_account"] and not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
        errors.append("bank account number must be 9 to 18 digits")
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        if age_on(dob, now_ist().date()) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


class DuplicateActiveApplication(Exception):
    """The database refused a second active application for the same mobile."""


def lock_mobile(cur, mobile):
    """Serialise submissions for one mobile number until the transaction ends, so a retried
    or double-clicked submission cannot slip past the one-active-application check."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("sewasetu-apply:" + mobile,))


def create_application(cur, mobile, cleaned, channel_note=""):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no). Application numbers are random, so a
    collision with one of the ~100k existing numbers is retried (a unique index enforces it).
    """
    for attempt in range(8):
        app_no = new_application_no()
        cur.execute("SAVEPOINT new_app")
        try:
            cur.execute(
                """INSERT INTO applications
                   (application_no, applicant_name, mobile, dob, gender, marital_status,
                    husband_name, husband_employer, village, block, bank_account, ifsc,
                    doc_path, status, submitted_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
                   RETURNING id""",
                (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
                 cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
                 cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
                 cleaned["doc_path"], datetime.now()))
            new_id = cur.fetchone()[0]
            cur.execute("RELEASE SAVEPOINT new_app")
            break
        except psycopg2.errors.UniqueViolation as e:
            cur.execute("ROLLBACK TO SAVEPOINT new_app")
            if "uq_active_application_per_mobile" in str(e):
                raise DuplicateActiveApplication(mobile)
            continue  # application number collision: draw another
    else:
        raise RuntimeError("could not allocate an application number")

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile, channel_note)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


DOC_SIGNATURES = {"pdf": (b"%PDF",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
                  "png": (b"\x89PNG",)}


def store_age_proof(mobile, filename, content):
    """Save an age-proof upload under a server-chosen name. Returns (path, problem)."""
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext not in DOC_SIGNATURES:
        return None, "Please upload a JPG, PNG or PDF file."
    if not content:
        return None, "The file is empty."
    if len(content) > 5 * 1024 * 1024:
        return None, "File is larger than 5 MB. Please upload a smaller file."
    if not any(content.startswith(sig) for sig in DOC_SIGNATURES[ext]):
        return None, "The file does not look like a real %s. Please upload a photo or PDF." % ext.upper()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = "%s_%s.%s" % (mobile, secrets.token_hex(8), "jpg" if ext == "jpeg" else ext)
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as out:
        out.write(content)
    return path, None


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        filename = f.filename.lower()
        content = f.read()
        if not filename.rsplit(".", 1)[-1] in ("pdf", "jpg", "jpeg", "png"):
            flash("Please upload a JPG, PNG or PDF file.")
            return render_template("upload.html")
        if len(content) > 5 * 1024 * 1024:
            flash("File is larger than 5 MB. Please upload a smaller file.")
            return render_template("upload.html")
        path, problem = store_age_proof(session["verified_mobile"], filename, content)
        if problem:
            flash(problem)
            return render_template("upload.html")
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    errors, cleaned = validate_application(data, session.get("doc_path", ""))
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("form_step", step=1))

    conn = get_db()
    cur = conn.cursor()
    lock_mobile(cur, mobile)
    existing = active_application(cur, mobile)
    if existing:
        conn.rollback(); cur.close(); conn.close()
        flash("Application %s (%s) already exists for this mobile number. A pending "
              "application can be withdrawn from the status portal if you need to "
              "apply afresh." % (existing[1], existing[2]))
        return redirect(url_for("index"))

    try:
        new_id, app_no = create_application(cur, mobile, cleaned, "web form")
    except DuplicateActiveApplication:
        conn.rollback(); cur.close(); conn.close()
        flash("An application already exists for this mobile number.")
        return redirect(url_for("index"))
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Officers. Each block officer has her own login, so a decision is recorded against her
# name, and she sees the queue for her block. The ADMIN_USERNAME account from the
# environment is the district login (all blocks).
# ---------------------------------------------------------------------------

from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402


def _district_officer():
    return {"username": ADMIN_USERNAME,
            "full_name": os.environ.get("ADMIN_FULL_NAME", "District Social Welfare Officer"),
            "block": None}


def officer_login(username, password):
    """Check departmental credentials. Returns the officer (dict) or None."""
    username = (username or "").strip()
    password = password or ""
    if ADMIN_PASSWORD and username == ADMIN_USERNAME and \
            secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return _district_officer()
    officer = get_officer(username, with_hash=True) if username != ADMIN_USERNAME else None
    if officer:
        pw_hash = officer.pop("password_hash") or ""
        if pw_hash and check_password_hash(pw_hash, password):
            return officer
    return None


def get_officer(username, with_hash=False):
    """The officer behind a username (or an `officer:<username>` token subject)."""
    username = (username or "").split(":", 1)[-1]
    if username == ADMIN_USERNAME:
        return _district_officer()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT username, full_name, block, password_hash FROM officers "
                    "WHERE username = %s AND active", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()
    except psycopg2.Error:
        return None
    if not row:
        return None
    out = {"username": row[0], "full_name": row[1], "block": row[2]}
    if with_hash:
        out["password_hash"] = row[3]
    return out


def officer_can_see(officer, block):
    return officer is not None and (officer["block"] is None or officer["block"] == block)


def decide_application(cur, app_id, new_status, officer, reason_code="", note="", channel=""):
    """Approve/reject a PENDING application for an officer. One code path for the web form
    and the officer's assistant. Returns (ok, message, application_no). Caller commits."""
    if new_status not in ("APPROVED", "REJECTED"):
        return False, "decision must be APPROVED or REJECTED", None
    cur.execute("SELECT application_no, block, status FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        return False, "no such application", None
    app_no, block, status = row
    if not officer_can_see(officer, block):
        return False, "application %s is in %s block, outside your jurisdiction" % (app_no, block), app_no
    if status != "PENDING":
        return False, "application %s is %s; only a pending application can be decided" % (app_no, status), app_no
    reason_code = (reason_code or "").strip().upper()
    if new_status == "REJECTED":
        if reason_code not in REJECTION_REASONS:
            return False, "a rejection needs a reason: one of " + ", ".join(REJECTION_REASONS), app_no
        if reason_code == "OTHER" and not (note or "").strip():
            return False, "reason OTHER needs a note explaining the rejection", app_no
    if new_status == "APPROVED":
        flags = review_flags(cur, app_id)
        blocking = [f for f in flags if f["severity"] == "block"]
        if blocking:
            return False, ("application %s cannot be approved: %s. It can be rejected (e.g. UNDER_AGE)."
                           % (app_no, "; ".join(f["detail"] for f in blocking))), app_no
        warns = [f for f in flags if f["severity"] == "warn"]
        if warns and not (note or "").strip():
            return False, ("application %s has warnings (%s). To approve anyway, give a note saying "
                           "what you verified." % (app_no, "; ".join(f["code"] for f in warns))), app_no
    parts = []
    if reason_code in REJECTION_REASONS and new_status == "REJECTED":
        parts.append(REJECTION_REASONS[reason_code])
    if (note or "").strip():
        parts.append(note.strip())
    parts.append("by %s" % officer["full_name"])
    if channel:
        parts.append(channel)
    audit_note = " | ".join(parts)[:500]
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
                (new_status, datetime.now(), officer["username"][:50], app_id))
    if not cur.fetchone():
        return False, "application %s was decided by someone else just now" % app_no, app_no
    write_audit(cur, app_id, new_status, ("admin:%s" % officer["username"])[:50], audit_note)
    return True, "Application %s %s." % (app_no, new_status.lower()), app_no


def review_flags(cur, app_id):
    """What an officer should know before deciding. Each flag: code, severity, detail.

    severity "block": the scheme rules are not met, so approval is refused on every channel;
    "warn": approval needs a note acknowledging it; "info": context only.
    """
    cur.execute("SELECT id, applicant_name, mobile, dob, village, block, bank_account, ifsc, "
                "doc_path, status, submitted_at, gender, marital_status, husband_name "
                "FROM applications WHERE id = %s", (app_id,))
    r = cur.fetchone()
    if not r:
        return []
    (aid, name, mobile, dob, village, block, bank, ifsc, doc, status, submitted,
     gender, marital, husband) = r
    flags = []
    if dob and submitted and age_on(dob, submitted.date()) < MIN_AGE:
        flags.append({"code": "UNDER_60_AT_SUBMISSION", "severity": "block",
                      "detail": "aged %d on the day of application (born %s); the scheme needs %d+"
                                % (age_on(dob, submitted.date()), dob.strftime("%d/%m/%Y"), MIN_AGE)})
    if not doc:
        flags.append({"code": "NO_AGE_PROOF", "severity": "block",
                      "detail": "no age proof document on file"})
    data = {"applicant_name": name, "village": village, "block": block, "bank_account": bank,
            "ifsc": ifsc, "dob": dob.strftime("%d/%m/%Y") if dob else "", "gender": gender,
            "marital_status": marital, "husband_name": husband}
    errors, _ = validate_application(data, doc or "x")
    for e in errors:
        if "window" in e or "years of age" in e or "age proof" in e:
            continue
        code = "IFSC_INVALID" if "IFSC" in e else "FIELD_PROBLEM"
        flags.append({"code": code, "severity": "warn", "detail": e + " (as currently stored: %s)"
                      % (ifsc if code == "IFSC_INVALID" else "see record")})
    if name and dob:
        cur.execute("SELECT application_no, status, block, mobile, village, gender, bank_account = %s, "
                    "submitted_at FROM applications WHERE id <> %s "
                    "AND lower(applicant_name) = lower(%s) AND dob = %s AND mobile <> %s "
                    "AND status IN ('PENDING','APPROVED','DEEMED_APPROVED') LIMIT 5",
                    (bank, aid, name, dob, mobile))
        for o_no, o_status, o_block, o_mobile, o_village, o_gender, o_same_bank, o_sub in cur.fetchall():
            match = ["same name", "same date of birth"]
            match += ["same bank account"] if o_same_bank else []
            match += ["same village"] if (o_village or "").lower() == (village or "").lower() else []
            match += ["same gender"] if o_gender == gender else ["different gender (%s)" % o_gender]
            flags.append({"code": "POSSIBLE_DUPLICATE_BENEFICIARY", "severity": "warn",
                          "detail": "%s as %s (%s since %s; %s, %s block; mobile XXXXXX%s). One person may hold "
                                    "only one pension." % (", ".join(match), o_no, o_status,
                                                            o_sub.strftime("%d %b %Y") if o_sub else "?",
                                                            o_village, o_block, o_mobile[-4:])})
    if bank:
        cur.execute("SELECT application_no, status, block, applicant_name FROM applications WHERE id <> %s "
                    "AND bank_account = %s AND mobile <> %s AND lower(applicant_name) <> lower(%s) "
                    "AND status IN ('PENDING','APPROVED','DEEMED_APPROVED') LIMIT 3",
                    (aid, bank, mobile, name or ""))
        for o_no, o_status, o_block, o_name in cur.fetchall():
            flags.append({"code": "SAME_BANK_ACCOUNT", "severity": "warn",
                          "detail": "bank account also used by %s (%s, %s block, applicant %s); the pension must "
                                    "go to the applicant's own account" % (o_no, o_status, o_block, o_name)})
    if status == "PENDING" and submitted:
        due = submitted + timedelta(days=SLA_DAYS)
        hours = (due - datetime.now()).total_seconds() / 3600
        if hours <= 48:
            flags.append({"code": "DEEMED_APPROVAL_SOON", "severity": "info",
                          "detail": ("SLA already passed; it will be deemed approved at the next nightly run"
                                     if hours <= 0 else
                                     "will be deemed approved automatically after %s if not decided"
                                     % due.strftime("%d %b %Y %H:%M"))})
    cur.execute("SELECT 1 FROM audit_log WHERE application_id = %s AND action = 'SUBMIT' "
                "AND note LIKE 'AI assistant%%'", (aid,))
    if cur.fetchone():
        flags.append({"code": "VIA_ASSISTANT", "severity": "info",
                      "detail": "submitted by the applicant through an AI assistant (OTP-verified mobile)"})
    return flags


def notify_decision(app_no, mobile, status, reason_code=""):
    """Tell the applicant the outcome (both channels: web and assistant)."""
    if status == "APPROVED":
        text = "Sewa Setu: your pension application %s has been approved." % app_no
    else:
        text = ("Sewa Setu: your pension application %s was not approved (%s). You may apply again."
                % (app_no, REJECTION_REASONS.get((reason_code or "").upper(), "see status portal")))
    send_sms(mobile, text)


def _session_officer():
    if not session.get("admin"):
        return None
    return {"username": session.get("admin_user"), "full_name": session.get("officer_name") or
            session.get("admin_user"), "block": session.get("officer_block")}


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        officer = officer_login(request.form.get("username"), request.form.get("password"))
        if officer:
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = officer["username"]
            session["officer_name"] = officer["full_name"]
            session["officer_block"] = officer["block"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
def admin_dashboard():
    officer = _session_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    blk = officer["block"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications WHERE (%s IS NULL OR block = %s) "
                "GROUP BY status", (blk, blk))
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s AND (%s IS NULL OR block = %s)",
                (datetime.now() - timedelta(days=SLA_DAYS), blk, blk))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "AND (%s IS NULL OR block = %s) GROUP BY block ORDER BY count(*) DESC", (blk, blk))
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS, officer=officer)


@app.route("/admin/applications")
def admin_list():
    officer = _session_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    blk = officer["block"]
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s "
                    "AND (%s IS NULL OR block = %s) ORDER BY submitted_at ASC",
                    (mobile, blk, blk))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "AND (%s IS NULL OR block = %s) "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, blk, blk, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    officer = _session_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row or not officer_can_see(officer, row[7]):
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    flags = review_flags(cur, app_id)
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit,
                           reasons=REJECTION_REASONS, flags=flags)


@app.route("/admin/document/<int:app_id>")
def admin_document(app_id):
    officer = _session_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT doc_path, block FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row or not row[0] or not officer_can_see(officer, row[1]):
        abort(404)
    real = os.path.realpath(row[0])
    if not real.startswith(os.path.realpath(UPLOAD_DIR) + os.sep) or not os.path.exists(real):
        abort(404)
    return send_file(real)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    officer = _session_officer()
    if not officer:
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    ok, msg, _ = decide_application(cur, app_id, new_status, officer,
                                    request.form.get("reason_code", ""), note, "web")
    if ok:
        conn.commit()
        cur.execute("SELECT application_no, mobile FROM applications WHERE id = %s", (app_id,))
        no, mob = cur.fetchone()
        notify_decision(no, mob, new_status, request.form.get("reason_code", ""))
    else:
        conn.rollback()
    flash(msg)
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


# ---------------------------------------------------------------------------
# Age-proof upload links for the AI-assistant channel. An assistant cannot carry a
# 5 MB photo through a chat, so it hands the applicant (or her grandson, or the CSC
# operator) a short-lived link bound to her mobile number. It works from a phone
# browser, and from a terminal: curl -F document=@voter-id.jpg <link>
# ---------------------------------------------------------------------------

UPLOAD_LINK_MINUTES = 30


def create_upload_link(cur, mobile):
    token = secrets.token_urlsafe(24)
    cur.execute("INSERT INTO upload_tokens (token_hash, mobile, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (hashlib.sha256(token.encode()).hexdigest(), mobile, datetime.now(),
                 datetime.now() + timedelta(minutes=UPLOAD_LINK_MINUTES)))
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return base + "/u/" + token


def _wants_json():
    accept = request.headers.get("Accept", "")
    return "text/html" not in accept


@app.route("/u/<token>", methods=["GET", "POST"])
def agent_upload(token):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT mobile, expires_at FROM upload_tokens WHERE token_hash = %s",
                (hashlib.sha256(token.encode()).hexdigest(),))
    row = cur.fetchone()
    if not row or row[1] < datetime.now():
        cur.close(); conn.close()
        msg = "This upload link has expired. Ask the assistant for a new one."
        if _wants_json():
            return {"ok": False, "error": msg}, 410
        return render_template("agent_upload.html", expired=True, message=msg), 410
    mobile = row[0]
    masked = "XXXXXX" + mobile[-4:]
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            problem = "Please choose a photo or PDF of the age proof."
            path = None
        else:
            path, problem = store_age_proof(mobile, f.filename, f.read())
        if problem:
            cur.close(); conn.close()
            if _wants_json():
                return {"ok": False, "error": problem}, 400
            return render_template("agent_upload.html", masked=masked, message=problem), 400
        cur.execute("INSERT INTO agent_drafts (mobile, data, doc_path, doc_name, updated_at) "
                    "VALUES (%s, '{}'::jsonb, %s, %s, %s) ON CONFLICT (mobile) DO UPDATE SET "
                    "doc_path = EXCLUDED.doc_path, doc_name = EXCLUDED.doc_name, "
                    "updated_at = EXCLUDED.updated_at",
                    (mobile, path, sanitize(f.filename, 100), datetime.now()))
        cur.execute("UPDATE upload_tokens SET used_at = %s WHERE token_hash = %s",
                    (datetime.now(), hashlib.sha256(token.encode()).hexdigest()))
        conn.commit(); cur.close(); conn.close()
        msg = ("Age proof received for mobile %s. Go back to the assistant and say it is "
               "uploaded." % masked)
        if _wants_json():
            return {"ok": True, "message": msg}
        return render_template("agent_upload.html", done=True, masked=masked, message=msg)
    cur.close(); conn.close()
    return render_template("agent_upload.html", masked=masked)


@app.route("/healthz")
def healthz():
    try:
        conn = get_db(); cur = conn.cursor(); cur.execute("SELECT 1"); cur.close(); conn.close()
        return {"ok": True}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}, 500


# ---------------------------------------------------------------------------
# /eval-dashboard: the invented citizens, their conversations with a real MCP client,
# and what was checked in this database afterwards. Results are JSON files written by
# eval/run_agent_evals.py and committed, so the page shows exactly what was run.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
import glob as _glob  # noqa: E402

EVAL_DIR = os.environ.get("EVAL_RESULTS_DIR",
                          os.path.join(os.path.dirname(BASE_DIR), "eval", "results"))


def _load_eval_runs():
    runs = []
    for path in sorted(_glob.glob(os.path.join(EVAL_DIR, "run-*.json")), reverse=True):
        try:
            with open(path, encoding="utf-8") as fh:
                runs.append(_json.load(fh))
        except (OSError, ValueError):
            continue
    personas, changes = [], []
    for name, target in (("personas.json", personas), ("changes.json", changes)):
        try:
            with open(os.path.join(EVAL_DIR, name), encoding="utf-8") as fh:
                target.extend(_json.load(fh))
        except (OSError, ValueError):
            pass
    contract = None
    try:
        with open(os.path.join(EVAL_DIR, "contract-latest.json"), encoding="utf-8") as fh:
            contract = _json.load(fh)
    except (OSError, ValueError):
        pass
    return runs, personas, changes, contract


@app.template_filter("mdlite")
def _mdlite(text):
    """Escape, then render **bold** and `code` from assistant messages."""
    from markupsafe import escape, Markup
    t = str(escape(text or ""))
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return Markup(t)


@app.route("/eval-dashboard")
def eval_dashboard():
    runs, personas, changes, contract = _load_eval_runs()
    sel = request.args.get("run")
    # default: the latest complete run (re-checks of a single persona are listed but not the headline)
    full = max((r.get("total", 0) for r in runs), default=0)
    default = next((r for r in runs if r.get("total", 0) == full), runs[0] if runs else None)
    run = next((r for r in runs if r.get("run_id") == sel), default)
    by_persona = {}
    if run:
        for res in run.get("results", []):
            by_persona.setdefault(res["persona"], []).append(res)
    return render_template("eval_dashboard.html", runs=runs, run=run, personas=personas,
                           changes=changes, contract=contract, by_persona=by_persona)


@app.route("/eval-dashboard/runs.json")
def eval_dashboard_json():
    runs, personas, changes, contract = _load_eval_runs()
    return {"runs": runs, "personas": personas, "changes": changes, "contract": contract}


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
