# -*- coding: utf-8 -*-
"""Capture real demo material from the running system: browser screenshots of the actual
portal pages and real Claude Code (Haiku) sessions against the MCP servers."""
import os
import re
import sys
import json
import time
import base64
import hashlib
import secrets
import asyncio
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/claude/sewasetu-assistant/eval"); sys.path.insert(0, "/home/claude/sewasetu-assistant/app")
from sewasetu_client import (PORTAL, CITIZEN_MCP, OFFICER_MCP, q, fresh_mobile, read_otp, _last_msg_id,  # noqa
                             make_age_proof, REDIRECT)
from run_agent_evals import assistant_turn  # noqa

A = "/home/claude/video/assets"

# a loopback callback listener, as a native OAuth client (Claude Code) runs
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
CALLBACKS = []


class _CB(BaseHTTPRequestHandler):
    def do_GET(self):
        CALLBACKS.append("http://localhost:33418" + self.path)
        body = ("<html><body style='font:22px Inter,sans-serif;padding:70px;color:#1b5e20'>"
                "Signed in to Sewa Setu. You can close this tab and return to Claude Code.</body></html>").encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", 33418), _CB).serve_forever, daemon=True).start()
os.makedirs(A, exist_ok=True)
VIEW = {"width": 1440, "height": 900}
out = {}


def shot(page, name, full=False):
    page.wait_for_timeout(350)
    page.screenshot(path=os.path.join(A, name + ".png"), full_page=full)
    print("shot", name)


def browser_oauth(ctx, role, resource, prefix, mobile=None, username=None, password=None):
    cid = requests.post(PORTAL + "/oauth/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude Code",
                                                          "token_endpoint_auth_method": "none"}).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = PORTAL + "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": resource, "scope": role, "state": "demo"})
    page = ctx.new_page()
    seen = CALLBACKS

    def _unused(route):
        route.fulfill(
        content_type="text/html",
        body="<html><body style='font:20px Inter;padding:60px;color:#1b5e20'>Signed in. You can close this tab and "
             "return to Claude Code.</body></html>")
    page.goto(url)
    if role == "citizen":
        page.fill("input[name=mobile]", mobile)
        shot(page, prefix + "_login_mobile")
        before = _last_msg_id(mobile)
        with page.expect_navigation():
            page.click("button[type=submit]")
        otp = read_otp(mobile, before)
        gw = ctx.new_page()
        gw.goto(PORTAL + "/__gateway/")
        shot(gw, prefix + "_gateway")
        gw.close()
        page.fill("input[name=otp]", otp)
        shot(page, prefix + "_login_otp")
        with page.expect_navigation():
            page.click("button[type=submit]")
    else:
        page.fill("input[name=username]", username)
        page.fill("input[name=password]", password)
        shot(page, prefix + "_login")
        with page.expect_navigation():
            page.click("button[type=submit]")
    shot(page, prefix + "_consent")
    page.click("button[value=allow]")
    for _ in range(40):
        if seen:
            break
        page.wait_for_timeout(250)
    code = parse_qs(urlparse(seen[-1]).query)["code"][0]
    page.close()
    tok = requests.post(PORTAL + "/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                       "code_verifier": verifier, "client_id": cid,
                                                       "redirect_uri": REDIRECT, "resource": resource}).json()
    return tok["access_token"]


def converse(workdir, server, url, token, messages, label):
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "mcp.json"), "w") as fh:
        json.dump({"mcpServers": {server: {"type": "http", "url": url,
                                           "headers": {"Authorization": "Bearer " + token}}}}, fh)
    sid, turns = None, []
    for m in messages:
        a = assistant_turn(m, workdir, sid, server)
        sid = a["session_id"]
        turns.append({"user": m, "assistant": a["all_text"], "error": a["error"],
                      "tools": [{"name": t["name"].split("__")[-1], "input": t["input"], "result": t["result"][:3000]}
                                for t in a["tools"] if t["name"] != "ToolSearch"]})
        print(label, "turn done:", [t["name"].split("__")[-1] for t in a["tools"]], (a["error"] or ""))
    with open(os.path.join(A, label + ".json"), "w") as fh:
        json.dump(turns, fh, indent=1, ensure_ascii=False)
    return turns


def run_citizen(ctx, mobile):
    ctok = browser_oauth(ctx, "citizen", CITIZEN_MCP, "c", mobile=mobile)
    work = "/home/claude/video/cc-citizen"
    os.makedirs(work, exist_ok=True)
    make_age_proof(os.path.join(work, "kamala_voter_id.jpg"), "Kamala Das", "12/03/1952")
    converse(work, "sewasetu", CITIZEN_MCP, ctok, [
        "hi, my nani needs the old age pension. can you apply for her? we signed in with her number",
        "Kamala Das, born 12/03/1952, female, widow (husband late Prafulla Das). village Lakhipur, Sonari block. "
        "bank account 30418822917, IFSC SBIN0003077. her voter id photo is kamala_voter_id.jpg in this folder",
        "yes everything is correct, nani agrees to the declaration. please submit",
        "thanks! when will she hear back?",
    ], "citizen_session")


def main():
    mobile = "9864012378"
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport=VIEW, device_scale_factor=1)
        # ---------------- citizen
        if not q("SELECT 1 FROM applications WHERE mobile = %s", (mobile,)):
            run_citizen(ctx, mobile)
        ctok = None
        work = "/home/claude/video/cc-citizen"
        row = q("SELECT id, application_no FROM applications WHERE mobile = %s AND status = 'PENDING'", (mobile,))
        assert row, "citizen session did not create an application"
        app_id, app_no = row[0]
        out["citizen"] = {"mobile": mobile, "application_no": app_no, "app_id": app_id}
        print("APPLICATION", app_no)
        # phone view of an upload link
        import app as portal
        conn = portal.get_db(); cur = conn.cursor()
        upload_link = portal.create_upload_link(cur, mobile)
        conn.commit(); cur.close(); conn.close()
        phone = b.new_context(viewport={"width": 390, "height": 760}, device_scale_factor=2, is_mobile=True)
        pp = phone.new_page()
        pp.goto(upload_link, wait_until="load")
        shot(pp, "c_upload_phone")
        phone.close()
        # status portal (the web channel sees the same record)
        sp = ctx.new_page()
        sp.goto(PORTAL + "/status")
        sp.fill("input[name=mobile]", mobile)
        sp.fill("input[name=password]", "12031952")
        with sp.expect_navigation():
            sp.click("button[type=submit]")
        shot(sp, "c_status")
        sp.close()
        # district admin view before decision
        ap = ctx.new_page()
        ap.goto(PORTAL + "/admin")
        ap.fill("input[name=username]", "admin")
        ap.fill("input[name=password]", os.environ["ADMIN_PASSWORD"])
        with ap.expect_navigation():
            ap.click("button[type=submit]")
        ap.goto(PORTAL + "/admin/application/%d" % app_id)
        shot(ap, "a_view_pending", full=True)
        ap.close()
        # ---------------- officer
        otok = browser_oauth(ctx, "officer", OFFICER_MCP, "o", username="rina.sonari", password="sonari-pass")
        converse("/home/claude/video/cc-officer", "sewasetu_officer", OFFICER_MCP, otok, [
            "What's waiting for me today? Newest first.",
            "Open Kamala Das's application and check it properly, including her age proof.",
            "Approve it. I have checked the voter ID against the form.",
            "What about the Haren Bora case in my queue?",
            "Reject it as a duplicate beneficiary.",
        ], "officer_session")
        rp = ctx.new_page()
        rp.goto(PORTAL + "/admin")
        rp.fill("input[name=username]", "rina.sonari")
        rp.fill("input[name=password]", "sonari-pass")
        with rp.expect_navigation():
            rp.click("button[type=submit]")
        shot(rp, "o_dashboard")
        rp.goto(PORTAL + "/admin/application/%d" % app_id)
        shot(rp, "a_view_approved", full=True)
        haren = q("SELECT id FROM applications WHERE applicant_name = 'Haren Bora' AND block = 'Sonari' "
                  "ORDER BY submitted_at DESC LIMIT 1")[0][0]
        rp.goto(PORTAL + "/admin/application/%d" % haren)
        shot(rp, "a_view_duplicate", full=True)
        rp.close()
        # ---------------- eval dashboard
        ep = ctx.new_page()
        ep.goto(PORTAL + "/eval-dashboard")
        shot(ep, "e_dashboard", full=True)
        ep.close()
        b.close()
    out["status"] = q("SELECT application_no, status, decided_by FROM applications WHERE mobile = %s", (mobile,))
    with open(os.path.join(A, "meta.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
