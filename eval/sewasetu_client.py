# -*- coding: utf-8 -*-
"""Helpers shared by the eval runners: real OAuth sign-in against the portal, DB access,
test documents, MCP client calls."""
import os
import re
import json
import time
import base64
import random
import hashlib
import secrets
from urllib.parse import urlparse, parse_qs, urlencode

import requests
import psycopg2

PORTAL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CITIZEN_MCP = os.environ.get("CITIZEN_MCP_URL", "http://localhost:8101/mcp")
OFFICER_MCP = os.environ.get("OFFICER_MCP_URL", "http://localhost:8102/mcp")
REDIRECT = "http://localhost:33418/callback"


def db():
    return psycopg2.connect(host=os.environ.get("DB_HOST", "127.0.0.1"), port=os.environ.get("DB_PORT", "5432"),
                            dbname=os.environ.get("DB_NAME", "sewasetu"), user=os.environ.get("DB_USER", "sewasetu"),
                            password=os.environ.get("DB_PASSWORD", "sewasetu123"))


def q(sql, args=()):
    conn = db(); cur = conn.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall() if cur.description else []
    conn.commit(); cur.close(); conn.close()
    return rows


def fresh_mobile():
    """A 10-digit mobile with no history in the portal."""
    while True:
        m = random.choice("6789") + "".join(random.choice("0123456789") for _ in range(9))
        if not q("SELECT 1 FROM applications WHERE mobile = %s UNION SELECT 1 FROM otps WHERE mobile = %s", (m, m)):
            return m


def read_otp(mobile, after_id=0, timeout=20):
    """Read the OTP SMS from the simulated gateway (as a person reads her phone)."""
    end = time.time() + timeout
    while time.time() < end:
        msgs = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
        for m in reversed(msgs):
            if m["id"] > after_id and m["delivered"] and m["text"] and "OTP" in m["text"]:
                return re.search(r"\b(\d{6})\b", m["text"]).group(1)
        time.sleep(0.7)
    raise TimeoutError("no OTP delivered to %s" % mobile)


def _last_msg_id(mobile):
    msgs = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
    return max([m["id"] for m in msgs] or [0])


def csrf(s, url):
    """Load a form page in this browser session and return its CSRF token."""
    m = re.search(r'name="csrf_token" value="([^"]+)"', s.get(url).text)
    return m.group(1) if m else ""


def sign_in(role, resource, mobile=None, username=None, password=None, client_name="Sewa Setu eval client",
            scope=None):
    """Full OAuth 2.1 authorization-code + PKCE flow, exactly as an MCP client drives it.
    Returns the token response (access_token, refresh_token, ...) plus client_id."""
    s = requests.Session()
    reg = s.post(PORTAL + "/oauth/register", json={"redirect_uris": [REDIRECT], "client_name": client_name,
                                                   "token_endpoint_auth_method": "none"}).json()
    cid = reg["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": challenge,
              "code_challenge_method": "S256", "resource": resource, "state": "xyz"}
    if scope is not None:
        params["scope"] = scope
    r = s.get(PORTAL + "/oauth/authorize", params=params, allow_redirects=False)
    loc = r.headers["Location"]
    if "error=" in loc:
        raise RuntimeError("authorize error: " + loc)
    rid = parse_qs(urlparse(loc).query)["rid"][0]
    login = PORTAL + "/oauth/login?rid=" + rid
    tok_ = csrf(s, login)
    if role == "citizen":
        before = _last_msg_id(mobile)
        s.post(login, data={"mobile": mobile, "csrf_token": tok_})
        otp = read_otp(mobile, before)
        r = s.post(login, data={"otp": otp, "csrf_token": tok_}, allow_redirects=False)
    else:
        r = s.post(login, data={"username": username, "password": password, "csrf_token": tok_},
                   allow_redirects=False)
    if r.status_code != 302 or "consent" not in r.headers.get("Location", ""):
        raise RuntimeError("login failed for %s" % (mobile or username))
    consent = PORTAL + "/oauth/consent?rid=" + rid
    r = s.post(consent, data={"decision": "allow", "csrf_token": csrf(s, consent)}, allow_redirects=False)
    code = parse_qs(urlparse(r.headers["Location"]).query)["code"][0]
    tok = s.post(PORTAL + "/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                "code_verifier": verifier, "client_id": cid,
                                                "redirect_uri": REDIRECT, "resource": resource}).json()
    tok["client_id"] = cid
    return tok


def revoke(refresh_token):
    requests.post(PORTAL + "/oauth/revoke", data={"token": refresh_token})


def make_age_proof(path, name, dob, kind="Elector Photo Identity Card"):
    """A fictional age proof (Purvanchal is an invented state) with the person's name and DOB.
    Deliberately plausible: an earlier fixture said 'TEST - NOT VALID' and the officer's assistant,
    which reads the image, rightly refused to approve it."""
    from PIL import Image, ImageDraw, ImageFont
    rnd = random.Random(name + dob)
    im = Image.new("RGB", (900, 560), (246, 241, 230))
    d = ImageDraw.Draw(im)
    try:
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
        mid = ImageFont.truetype("DejaVuSans.ttf", 26)
        sm = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        big = mid = sm = ImageFont.load_default()
    d.rectangle([0, 0, 900, 78], fill=(120, 30, 40))
    d.text((24, 10), "STATE ELECTION OFFICE, PURVANCHAL", fill="white", font=big)
    d.text((24, 46), kind, fill=(255, 230, 200), font=sm)
    d.text((24, 100), "No. PVL%07d" % rnd.randint(0, 9999999), fill=(60, 60, 60), font=mid)
    d.text((24, 170), "Name: %s" % name, fill="black", font=big)
    d.text((24, 230), "Date of Birth: %s" % dob, fill="black", font=big)
    d.text((24, 290), "Address: as per electoral roll", fill=(40, 40, 40), font=mid)
    d.text((24, 470), "Electoral Registration Officer", fill=(60, 60, 60), font=sm)
    d.rectangle([660, 150, 860, 400], outline=(80, 80, 80), width=3, fill=(220, 214, 200))
    d.ellipse([715, 185, 805, 275], fill=(170, 160, 150))
    d.rectangle([700, 285, 820, 390], fill=(170, 160, 150))
    im.save(path, "JPEG", quality=85)
    return path


def upload_via_link(link, path):
    with open(path, "rb") as fh:
        return requests.post(link, files={"document": (os.path.basename(path), fh, "image/jpeg")},
                             headers={"Accept": "application/json"}, timeout=20).json()
