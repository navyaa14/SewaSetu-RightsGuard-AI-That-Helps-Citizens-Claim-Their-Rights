# -*- coding: utf-8 -*-
"""Sewa Setu - citizen MCP server (Old Age Pension).

Lets any AI assistant help a citizen check eligibility, apply, upload her age proof,
follow up on her application and withdraw it, acting only for the mobile number she
signed in with (OAuth, OTP to that mobile). Every rule is the portal's own:
validate_application() and create_application() in app.py, the same functions the
web form uses. Nothing here can create an application the counter would refuse.

Run:  uvicorn mcp_servers.citizen:app --host 0.0.0.0 --port 8101
"""
import os
import re
import json
from datetime import datetime, timedelta, date

from mcp.server.fastmcp import FastMCP, Context

from mcp_servers.common import (portal, get_auth, build_asgi, mask_account, mask_mobile,
                                fmt_date, log)

PUBLIC_URL = os.environ.get("CITIZEN_MCP_URL", "http://localhost:8101/mcp").rstrip("/")

DECLARATION = ("I hereby declare that the information furnished above is true to the best of my "
               "knowledge and belief. I understand that furnishing false information is punishable "
               "under applicable law and will result in cancellation of pension.")

INSTRUCTIONS = """Sewa Setu is the official Old Age Pension portal of the Government of Purvanchal
(Rs 250 a month for residents aged 60+). These tools act for the person who signed in, using
the mobile number she verified by OTP. That mobile becomes the applicant's mobile and her
status-portal login, so sign in with the APPLICANT'S own number (not a helper's).

Typical flow (few calls):
1. my_applications - see if she already has an application (status, decision deadline).
2. save_application_details - pass everything you know in ONE call. It normalises values,
   reports what is missing or wrong in plain words, and returns an upload link for her age proof.
3. Age proof: give her the upload link (opens on a phone), or if you can run commands and she
   gave you a file, run the curl command it returns. Never invent a document.
4. Read the details back to her, get a clear yes, then submit_application.
Rules come from the portal: never guess a date of birth, bank account or IFSC. If she does not
know a value, ask her to read it from her document or passbook. The portal decides; do not
promise approval. Older users may be slow, use simple words, and may speak Assamese, Bengali or Hindi:
reply in her language."""

mcp = FastMCP("Sewa Setu - Old Age Pension (citizen)", instructions=INSTRUCTIONS,
              stateless_http=True, json_response=True)

# ---------------------------------------------------------------------------
# normalisation (so a small model can pass what the person said)
# ---------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def normalise_dob(raw):
    """Returns (DD/MM/YYYY or None, human reading, problem)."""
    if raw is None:
        return None, None, None
    t = str(raw).strip().lower().replace(",", " ")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    if not t:
        return None, None, None
    d = None
    m = re.fullmatch(r"(\d{1,2})\s*[/.\- ]\s*(\d{1,2})\s*[/.\- ]\s*(\d{4})", t)
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))  # Indian order: day first
        except ValueError:
            return None, None, "'%s' is not a real date (day/month/year)" % raw
    m2 = re.fullmatch(r"(\d{4})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})", t)
    if not d and m2:
        try:
            d = date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            return None, None, "'%s' is not a real date" % raw
    if not d:
        parts = t.split()
        day = mon = yr = None
        for p in parts:
            if p.isdigit() and len(p) == 4:
                yr = int(p)
            elif p.isdigit() and len(p) <= 2:
                day = int(p)
            elif p[:3] in MONTHS:
                mon = MONTHS[p[:3]]
        if day and mon and yr:
            try:
                d = date(yr, mon, day)
            except ValueError:
                return None, None, "'%s' is not a real date" % raw
    if not d:
        if re.fullmatch(r"\d{4}", t) or "year" in t or "age" in t or "about" in t:
            return None, None, ("an exact date of birth is needed, as written on her age proof "
                                "(birth certificate, school certificate or Voter ID); an age or year is not enough")
        return None, None, "could not read '%s' as a date; give it as DD/MM/YYYY" % raw
    if d > date.today():
        return None, None, "date of birth %s is in the future" % d.strftime("%d/%m/%Y")
    if d.year < 1900:
        return None, None, "date of birth %s looks wrong" % d.strftime("%d/%m/%Y")
    return d.strftime("%d/%m/%Y"), d.strftime("%d %B %Y"), None


def normalise_choice(raw, table):
    if raw is None:
        return None
    t = str(raw).strip().lower()
    for canonical, words in table.items():
        if t == canonical.lower() or t in words:
            return canonical
    return str(raw).strip()


GENDERS = {"Male": {"m", "man", "male", "purush", "purus", "mard", "gent", "পুৰুষ", "पुरुष"},
           "Female": {"f", "woman", "female", "mahila", "lady", "aurat", "মহিলা", "महिला"},
           "Other": {"other", "transgender", "third gender"}}
MARITAL = {"Married": {"married", "vivahit", "shadi shuda"},
           "Unmarried": {"unmarried", "single", "never married", "not married", "avivahit"},
           "Widowed": {"widowed", "widow", "widower", "vidhwa", "bidhwa", "bidhoba", "বিধৱা", "विधवा"}}

_VILLAGES = {}


def village_block(village):
    """Known village -> block, learned from the portal's own records."""
    if not _VILLAGES:
        try:
            conn = portal.get_db(); cur = conn.cursor()
            cur.execute("SELECT lower(village), block, count(*) FROM applications GROUP BY 1, 2 "
                        "HAVING count(*) > 50")
            for v, b, _ in cur.fetchall():
                _VILLAGES[v] = b
            cur.close(); conn.close()
        except Exception as e:
            log.warning("village map: %s", e)
    return _VILLAGES.get((village or "").strip().lower())


def _status_words(status):
    return {
        "PENDING": "submitted, waiting for the block officer's decision",
        "APPROVED": "approved by the block officer; pension of Rs 250/month will be credited to the bank account",
        "DEEMED_APPROVED": ("approved automatically under the Purvanchal Right to Public Services Act "
                            "(no decision within 15 days); pension will be credited"),
        "REJECTED": "rejected by the block officer",
        "WITHDRAWN": "withdrawn by the applicant",
    }.get(status, status)


def _rights_clock(status, submitted, decided):
    if not submitted:
        return None
    due = submitted + timedelta(days=portal.SLA_DAYS)
    out = {"submitted_on": fmt_date(submitted), "decision_due_by": fmt_date(due)}
    if status == "PENDING":
        days = (due.date() - datetime.now().date()).days
        out["days_left_for_decision"] = max(days, 0)
        out["if_no_decision"] = ("If the block officer does not decide by %s, the application stands "
                                 "approved automatically (deemed approval, Purvanchal RTPS Act) in the "
                                 "nightly run after that date." % fmt_date(due))
    elif decided:
        out["decided_on"] = fmt_date(decided)
    return out


def _deadline_info():
    left = portal.deadline_remaining()
    return {"closes": portal.SCHEME_DEADLINE.strftime("%d %B %Y, %I:%M %p IST"),
            "open": left is not None, "hours_left": left}


def _db():
    conn = portal.get_db()
    return conn, conn.cursor()


def _load_draft(cur, mobile):
    cur.execute("SELECT data, doc_path, doc_name FROM agent_drafts WHERE mobile = %s", (mobile,))
    row = cur.fetchone()
    if not row:
        return {}, None, None
    return dict(row[0] or {}), row[1], row[2]


def _upload_instructions(cur, mobile):
    link = portal.create_upload_link(cur, mobile)
    return {
        "upload_link": link,
        "valid_for_minutes": portal.UPLOAD_LINK_MINUTES,
        "how": ("Send her this link: she (or a family member) opens it on the phone and takes a photo "
                "of her birth certificate, school leaving certificate or Voter ID. If you can run shell "
                "commands and have the file, run the curl command instead."),
        "curl": 'curl -sS -H "Accept: application/json" -F "document=@/path/to/age-proof.jpg" %s' % link,
    }


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
def scheme_info(ctx: Context) -> dict:
    """Official, current facts about the Old Age Pension scheme from the portal itself:
    amount, age rule, documents, deadline, processing time, which blocks this assistant
    channel serves. Use this instead of anything you remember about the scheme."""
    auth = get_auth(ctx)
    return {
        "scheme": "Old Age Pension Scheme, Department of Social Welfare, Government of Purvanchal",
        "amount": "Rs 250 per month, paid by DBT into the applicant's own bank account",
        "who_can_apply": "residents of Purvanchal aged %d or above on the day of applying" % portal.MIN_AGE,
        "one_application": ("one active application per mobile number. Two people cannot share one "
                            "mobile number for two applications; each needs her own number"),
        "documents": ["one age proof: birth certificate, school leaving certificate or Voter ID "
                      "(photo or PDF, up to 5 MB)", "bank account number and IFSC (from the passbook)"],
        "application_window": _deadline_info(),
        "processing": ("a block officer decides within %d days; if not, the application stands approved "
                       "automatically (Purvanchal Right to Public Services Act)" % portal.SLA_DAYS),
        "blocks": portal.BLOCKS,
        "blocks_served_by_assistant": portal.AGENT_BLOCKS,
        "other_blocks": ("applicants from other blocks apply on the portal website or at the Block "
                         "Development Office; same scheme, same rules"),
        "signed_in_mobile": mask_mobile(auth.principal),
        "status_portal": "%s/status (mobile number + date of birth as DDMMYYYY)" % portal_base(),
        "help": "Block Development Office of her block",
    }


def portal_base():
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


@mcp.tool()
def check_eligibility(ctx: Context, date_of_birth: str, block: str = "") -> dict:
    """Quick check before applying: is a person with this date of birth (and block) able to
    apply today with the signed-in mobile? date_of_birth as written on the age proof, any common
    format (DD/MM/YYYY preferred). Does not save anything. The final check happens at submission."""
    auth = get_auth(ctx)
    mobile = auth.principal
    dob_s, reading, problem = normalise_dob(date_of_birth)
    out = {"application_window": _deadline_info()}
    if problem:
        out.update({"can_apply_now": False, "problem": problem})
        return out
    dob = datetime.strptime(dob_s, "%d/%m/%Y").date()
    today = portal.now_ist().date()
    age = portal.age_on(dob, today)
    try:
        sixty = dob.replace(year=dob.year + portal.MIN_AGE)
    except ValueError:  # 29 Feb
        sixty = date(dob.year + portal.MIN_AGE, 3, 1)
    reasons = []
    out.update({"date_of_birth_read_as": reading, "age_today": age})
    if age < portal.MIN_AGE:
        deadline = portal.SCHEME_DEADLINE.date()
        reasons.append("she is %d; the scheme starts at %d" % (age, portal.MIN_AGE))
        out["turns_60_on"] = fmt_date(sixty)
        out["can_apply_from"] = (fmt_date(sixty) + (" (before the window closes on %s)" % fmt_date(deadline)
                                 if sixty <= deadline else
                                 ", but the current application window closes on %s, before that; "
                                 "she should ask the Block Development Office about the next window" % fmt_date(deadline)))
    blk = portal.canonical_block(block) if block else ""
    if blk:
        if blk not in portal.BLOCKS:
            reasons.append("'%s' is not a block of this scheme (%s)" % (block, ", ".join(portal.BLOCKS)))
        elif blk not in portal.AGENT_BLOCKS:
            reasons.append("%s block is not yet served through assistants; apply on the portal website "
                           "or at the Block Development Office" % blk)
    if portal.deadline_remaining() is None:
        reasons.append("the application window has closed")
    conn, cur = _db()
    try:
        existing = portal.active_application(cur, mobile)
    finally:
        cur.close(); conn.close()
    if existing:
        reasons.append("this mobile already has application %s (%s)" % (existing[1], existing[2]))
        out["existing_application"] = {"application_no": existing[1], "status": existing[2]}
    out["can_apply_now"] = not reasons
    out["reasons"] = reasons or ["meets the age rule; she can apply now"]
    return out


@mcp.tool()
def save_application_details(ctx: Context,
                             applicant_name: str = None,
                             date_of_birth: str = None,
                             gender: str = None,
                             marital_status: str = None,
                             late_husband_name: str = None,
                             village: str = None,
                             block: str = None,
                             bank_account_number: str = None,
                             ifsc: str = None,
                             start_over: bool = False) -> dict:
    """Save (or correct) the pension application details for the signed-in mobile. Pass every
    field you know in one call; omit fields you do not know yet (never guess them). Values are
    normalised (any date format, 'sonari', 'widow'...), checked with the portal's own rules, and
    kept on the server so the application can be finished later. Returns what is still missing
    or wrong, an age-proof upload link if none is uploaded yet, and a read-back to confirm with
    her before calling submit_application.
    applicant_name: as on the age proof. date_of_birth: as on the age proof.
    gender: Male/Female/Other. marital_status: Married/Unmarried/Widowed.
    late_husband_name: only if widowed (optional). village: village or town.
    block: one of Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua (can be inferred from a
    known village). bank_account_number and ifsc: from her own passbook.
    start_over=true clears the saved details first."""
    auth = get_auth(ctx)
    mobile = auth.principal
    conn, cur = _db()
    try:
        if start_over:
            cur.execute("DELETE FROM agent_drafts WHERE mobile = %s", (mobile,))
        data, doc_path, doc_name = _load_draft(cur, mobile)
        notes = []
        if applicant_name is not None:
            data["applicant_name"] = applicant_name.strip()
        if date_of_birth is not None:
            dob_s, reading, problem = normalise_dob(date_of_birth)
            if problem:
                notes.append("date of birth not saved: " + problem)
            else:
                data["dob"] = dob_s
                if dob_s != str(date_of_birth).strip():
                    notes.append("date of birth read as %s (day/month/year); confirm with her" % reading)
        if gender is not None:
            data["gender"] = normalise_choice(gender, GENDERS)
        if marital_status is not None:
            data["marital_status"] = normalise_choice(marital_status, MARITAL)
        if late_husband_name is not None:
            data["husband_name"] = late_husband_name.strip()
        if village is not None:
            data["village"] = village.strip()
        if block is not None:
            data["block"] = portal.canonical_block(block)
        if not data.get("block") and data.get("village"):
            guess = village_block(data["village"])
            if guess:
                data["block"] = guess
                notes.append("block set to %s from the village name; confirm with her" % guess)
        if bank_account_number is not None:
            data["bank_account"] = re.sub(r"[\s-]", "", str(bank_account_number))
        if ifsc is not None:
            data["ifsc"] = re.sub(r"[\s-]", "", str(ifsc)).upper()
        cur.execute("INSERT INTO agent_drafts (mobile, data, doc_path, doc_name, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (mobile) DO UPDATE SET data = EXCLUDED.data, "
                    "updated_at = EXCLUDED.updated_at",
                    (mobile, json.dumps(data), doc_path, doc_name, datetime.now()))
        result = _draft_report(cur, mobile, data, doc_path, doc_name)
        result["notes"] = notes + result.get("notes", [])
        conn.commit()
        return result
    finally:
        cur.close(); conn.close()


def _draft_report(cur, mobile, data, doc_path, doc_name):
    errors, cleaned = portal.validate_application(data, doc_path or "")
    labels = [("applicant_name", "full name (as on age proof)"), ("dob", "date of birth"),
              ("gender", "gender"), ("marital_status", "marital status"), ("village", "village"),
              ("block", "block"), ("bank_account", "bank account number"), ("ifsc", "IFSC code")]
    missing = [label for key, label in labels if not data.get(key)]
    problems = []
    for e in errors:
        if e.endswith("is required") or e == "age proof document is required":
            continue
        if e.startswith("date of birth must be") and not data.get("dob"):
            continue
        problems.append(e)
    notes = []
    if data.get("gender") and data["gender"] not in GENDERS:
        problems.append("gender should be Male, Female or Other")
    if data.get("marital_status") and data["marital_status"] not in MARITAL:
        problems.append("marital status should be Married, Unmarried or Widowed")
    if data.get("ifsc", "").startswith("UTBI"):
        notes.append("United Bank of India IFSC codes were replaced after its merger into Punjab National "
                     "Bank; ask her to check the current IFSC printed in a recent passbook or with the branch")
    if any("IFSC" in p for p in problems):
        notes.append("IFSC is 11 characters: 4 letters, then 0, then 6 letters or digits (e.g. SBIN0003077)")
    blk = cleaned.get("block")
    if blk in portal.BLOCKS and blk not in portal.AGENT_BLOCKS:
        problems.append("%s block is not yet served through assistants: she can apply on the portal "
                        "website (%s/apply) or at the Block Development Office" % (blk, portal_base()))
    existing = portal.active_application(cur, mobile)
    report = {
        "saved_for_mobile": mask_mobile(mobile),
        "details": {
            "applicant_name": data.get("applicant_name"), "date_of_birth": data.get("dob"),
            "gender": data.get("gender"), "marital_status": data.get("marital_status"),
            "late_husband_name": data.get("husband_name") or None, "village": data.get("village"),
            "block": data.get("block"), "bank_account_number": data.get("bank_account"),
            "ifsc": data.get("ifsc")},
        "age_proof": ("uploaded (%s)" % (doc_name or "document")) if doc_path else "not uploaded yet",
        "missing": missing,
        "problems": problems,
    }
    if existing:
        report["problems"].insert(0, "this mobile already has application %s (%s); a new one cannot be "
                                     "submitted%s" % (existing[1], existing[2],
                                                      " unless that pending one is withdrawn first"
                                                      if existing[2] == "PENDING" else ""))
    if not doc_path:
        report["age_proof_upload"] = _upload_instructions(cur, mobile)
    ready = not missing and not report["problems"] and bool(doc_path)
    report["ready_to_submit"] = ready
    report["application_window"] = _deadline_info()
    if ready:
        report["next_step"] = ("Read these details back to her slowly (name, date of birth, village, block, "
                               "bank account, IFSC) and the declaration. If she says yes, call "
                               "submit_application with confirmed=true.")
        report["declaration"] = DECLARATION
    else:
        todo = []
        if missing:
            todo.append("ask her for: " + ", ".join(missing))
        if report["problems"]:
            todo.append("fix: " + "; ".join(report["problems"]))
        if not doc_path:
            todo.append("get her age proof uploaded with the link")
        report["next_step"] = "; ".join(todo)
    report["notes"] = notes
    return report


@mcp.tool()
def get_age_proof_upload_link(ctx: Context) -> dict:
    """A fresh link for uploading her age proof (birth certificate, school leaving certificate or
    Voter ID; JPG/PNG/PDF up to 5 MB), valid 30 minutes, tied to the signed-in mobile. She opens
    it on her phone, or you run the returned curl command if you have the file. Afterwards call
    save_application_details (with no fields) to confirm it arrived."""
    auth = get_auth(ctx)
    conn, cur = _db()
    try:
        out = _upload_instructions(cur, auth.principal)
        conn.commit()
        return out
    finally:
        cur.close(); conn.close()


@mcp.tool()
def submit_application(ctx: Context, confirmed: bool = False) -> dict:
    """Submit the saved application to the portal. Only call after you have read the details and
    the declaration back to her and she clearly agreed; then pass confirmed=true. The portal runs
    its own checks (age 60+, required fields, age proof, window open, one active application per
    mobile). Safe to call again: it never creates a second application. Returns the application
    number and the date by which a decision is due."""
    auth = get_auth(ctx)
    mobile = auth.principal
    if not confirmed:
        return {"submitted": False,
                "message": "Read the details and declaration back to her first; call again with confirmed=true "
                           "once she agrees.", "declaration": DECLARATION}
    conn, cur = _db()
    try:
        portal.lock_mobile(cur, mobile)
        existing = portal.active_application(cur, mobile)
        data, doc_path, doc_name = _load_draft(cur, mobile)
        if existing:
            conn.rollback()
            same = _matches_existing(mobile, existing[0], data)
            return {"submitted": same, "already_submitted": True, "application_no": existing[1],
                    "status": existing[2],
                    "message": ("This application was already submitted as %s; nothing new was created."
                                % existing[1]) if same else
                               ("This mobile already has application %s (%s). One mobile can hold only one "
                                "active application.%s" % (existing[1], existing[2],
                                 " If it was a mistake, it can be withdrawn and a fresh one submitted."
                                 if existing[2] == "PENDING" else ""))}
        if not data:
            conn.rollback()
            return {"submitted": False, "message": "No details saved yet. Call save_application_details first."}
        blk = portal.canonical_block(data.get("block"))
        if blk in portal.BLOCKS and blk not in portal.AGENT_BLOCKS:
            conn.rollback()
            return {"submitted": False,
                    "message": "%s block is not yet served through assistants. She can apply on the portal "
                               "website (%s/apply) or at the Block Development Office." % (blk, portal_base())}
        errors, cleaned = portal.validate_application(data, doc_path or "")
        if errors:
            conn.rollback()
            report = _draft_report(cur, mobile, data, doc_path, doc_name)
            return {"submitted": False, "portal_said": errors, "next_step": report["next_step"],
                    "age_proof_upload": report.get("age_proof_upload")}
        client = (auth.get("client_id") or "")
        app_id, app_no = portal.create_application(
            cur, mobile, cleaned, ("AI assistant (%s)" % client_label(client))[:500])
        cur.execute("DELETE FROM agent_drafts WHERE mobile = %s", (mobile,))
        conn.commit()
    except portal.DuplicateActiveApplication:
        conn.rollback()
        cur2 = conn.cursor()
        existing = portal.active_application(cur2, mobile)
        cur2.close()
        return {"submitted": True, "already_submitted": True,
                "application_no": existing[1] if existing else None,
                "message": "This application was already submitted; nothing new was created."}
    finally:
        cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                            "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    submitted = portal.now_ist()
    log.info("submitted %s for %s via %s", app_no, mask_mobile(mobile), client)
    return {
        "submitted": True, "application_no": app_no, "status": "PENDING",
        "status_meaning": _status_words("PENDING"),
        "rights_clock": _rights_clock("PENDING", submitted, None),
        "sms": "An SMS with the application number has been sent to her mobile.",
        "track": "Ask this assistant, or use %s/status with her mobile number and her date of birth "
                 "as DDMMYYYY." % portal_base(),
        "message": "Application %s submitted. Please note this number." % app_no,
    }


def client_label(client_id):
    if not client_id:
        return "assistant"
    try:
        conn, cur = _db()
        cur.execute("SELECT client_name FROM oauth_clients WHERE client_id = %s", (client_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0]:
            return row[0][:60]
    except Exception:
        pass
    return client_id[:40]


def _matches_existing(mobile, app_id, data):
    """Was the active application created from this same draft (i.e. a retried submit)?"""
    try:
        conn, cur = _db()
        cur.execute("SELECT a.applicant_name, a.dob, a.submitted_at, "
                    "EXISTS (SELECT 1 FROM audit_log l WHERE l.application_id = a.id AND l.action = 'SUBMIT' "
                    "AND l.note LIKE 'AI assistant%%') FROM applications a WHERE a.id = %s", (app_id,))
        name, dob, submitted, via_assistant = cur.fetchone()
        cur.close(); conn.close()
    except Exception:
        return False
    if not data:
        # the draft is deleted on success, so a retried submit finds no draft: it is a retry only
        # if that application came through an assistant in the last few hours
        return bool(via_assistant and submitted and datetime.now() - submitted < timedelta(hours=3))
    return (name or "").strip().lower() == (data.get("applicant_name") or "").strip().lower() and \
        bool(dob) and dob.strftime("%d/%m/%Y") == data.get("dob")


@mcp.tool()
def my_applications(ctx: Context) -> dict:
    """The signed-in mobile's pension applications (newest first): status in plain words, the
    decision deadline and deemed-approval date, the officer's reason if rejected, and what she can
    do next. Also shows any unfinished application details saved earlier. Use this to follow up."""
    auth = get_auth(ctx)
    mobile = auth.principal
    conn, cur = _db()
    try:
        cur.execute("SELECT id, application_no, applicant_name, status, submitted_at, decided_at, block, "
                    "bank_account FROM applications WHERE mobile = %s ORDER BY submitted_at DESC", (mobile,))
        apps = []
        for aid, no, name, status, sub, dec, blk, bank in cur.fetchall():
            item = {"application_no": no, "applicant_name": name, "block": blk, "status": status,
                    "meaning": _status_words(status), "rights_clock": _rights_clock(status, sub, dec),
                    "bank_account": mask_account(bank)}
            if status == "REJECTED":
                cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = 'REJECTED' "
                            "ORDER BY at DESC LIMIT 1", (aid,))
                r = cur.fetchone()
                item["reason"] = (r[0] if r and r[0] else "no reason recorded")
                item["next_step"] = "She may submit a fresh application with the problem corrected."
            elif status == "PENDING":
                item["next_step"] = "Nothing to do; wait for the decision. It can be withdrawn if it was a mistake."
                item["can_withdraw"] = True
            elif status == "WITHDRAWN":
                item["next_step"] = "She may submit a fresh application."
            apps.append(item)
        data, doc_path, _ = _load_draft(cur, mobile)
        out = {"mobile": mask_mobile(mobile), "applications": apps}
        if not apps:
            out["message"] = "No application has been made with this mobile number yet."
        if data or doc_path:
            out["unfinished_draft"] = {"fields_saved": sorted(data.keys()), "age_proof_uploaded": bool(doc_path),
                                       "next_step": "call save_application_details to see what is missing"}
        return out
    finally:
        cur.close(); conn.close()


@mcp.tool()
def withdraw_application(ctx: Context, application_no: str, confirm_application_no: str) -> dict:
    """Withdraw her PENDING application (for example, to correct a mistake and apply afresh).
    This cannot be undone. Ask her to confirm first, and pass the same application number in both
    fields. Approved or deemed-approved applications cannot be withdrawn here."""
    auth = get_auth(ctx)
    mobile = auth.principal
    a = (application_no or "").strip().upper()
    if a != (confirm_application_no or "").strip().upper():
        return {"withdrawn": False, "message": "The two application numbers do not match; nothing was changed."}
    conn, cur = _db()
    try:
        cur.execute("SELECT id, status FROM applications WHERE application_no = %s AND mobile = %s", (a, mobile))
        row = cur.fetchone()
        if not row:
            return {"withdrawn": False,
                    "message": "No application %s belongs to the signed-in mobile." % a}
        if row[1] != "PENDING":
            return {"withdrawn": False, "status": row[1],
                    "message": "Application %s is %s; only a pending application can be withdrawn." % (a, row[1])}
        # same statement as the status portal's withdraw button
        cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
                    "WHERE id = %s AND mobile = %s AND status = 'PENDING' RETURNING application_no",
                    (datetime.now(), row[0], mobile))
        if not cur.fetchone():
            conn.rollback()
            return {"withdrawn": False, "message": "It was decided just now and can no longer be withdrawn."}
        portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s" % mobile,
                           "AI assistant (%s)" % client_label(auth.get("client_id")))
        conn.commit()
        return {"withdrawn": True, "application_no": a,
                "message": "Application %s has been withdrawn. She may submit a new application." % a}
    finally:
        cur.close(); conn.close()


app = build_asgi(mcp, PUBLIC_URL, "citizen", "Sewa Setu citizen assistant")
