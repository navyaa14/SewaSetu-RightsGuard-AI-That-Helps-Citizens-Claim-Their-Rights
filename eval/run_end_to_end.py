# -*- coding: utf-8 -*-
"""One recorded end-to-end run of the whole service, as the assessors will exercise it.

  1. Citizen sign-in with an independent, standard MCP OAuth client (the official MCP Python SDK's
     OAuthClientProvider): it starts from the server's 401, discovers the protected-resource and
     authorization-server metadata, registers itself, runs PKCE, and a real browser completes the
     portal's mobile + OTP + consent pages.
  2. Claude Code (Haiku) applies for the citizen through the citizen MCP.
  3. The row is checked in the original PostgreSQL database (100k-row seed).
  4. Officer sign-in with the same standard client against the officer endpoint (scope discovered
     from that endpoint's challenge; a departmental login in the browser).
  5. Claude Code (Haiku) as the block officer reviews and approves it.
  6. Claude Code (Haiku) as the citizen asks for the status; the status portal shows the same.
  7. The citizen signs out; the next MCP call is refused.

    python eval/run_end_to_end.py        (stack running; claude CLI signed in)

Writes eval/results/e2e-latest.json, shown on /eval-dashboard and /demo.
"""
import os
import re
import sys
import json
import time
import asyncio
import tempfile
import threading
import subprocess
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from pydantic import AnyUrl
from playwright.async_api import async_playwright
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientMetadata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sewasetu_client import (PORTAL, CITIZEN_MCP, OFFICER_MCP, q, fresh_mobile, read_otp, _last_msg_id,  # noqa: E402
                             make_age_proof, csrf)
from run_agent_evals import assistant_turn  # noqa: E402
from redact import redact  # noqa: E402

CALLBACK_PORT = 3030
OFFICER = ("rina.sonari", "sonari-pass", "Rina Gogoi")
STEPS = []
_codes = []


class _CB(BaseHTTPRequestHandler):
    def do_GET(self):
        _codes.append(self.path)
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<p>Signed in. You can return to your assistant.</p>")

    def log_message(self, *a):
        pass


def step(name, ok, evidence):
    STEPS.append({"step": name, "passed": bool(ok), "evidence": evidence, "at": datetime.now().isoformat(timespec="seconds")})
    print("%s %s :: %s" % ("PASS" if ok else "FAIL", name, json.dumps(evidence, ensure_ascii=False)[:300]), flush=True)
    return ok


class Mem(TokenStorage):
    def __init__(self):
        self.tokens = None; self.client = None

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, t): self.tokens = t
    async def get_client_info(self): return self.client
    async def set_client_info(self, c): self.client = c


async def standard_oauth(server_url, role, mobile=None, username=None, password=None):
    """Sign in with the SDK's OAuth client; a browser completes the portal's pages."""
    storage, seen = Mem(), {}
    pw = await async_playwright().start()
    browser = await pw.chromium.launch()

    async def on_redirect(url):
        seen["authorize_url"] = url
        qs = parse_qs(urlparse(url).query)
        seen["scope_requested"] = qs.get("scope", [""])[0]
        seen["resource"] = qs.get("resource", [""])[0]
        page = await browser.new_page()
        await page.goto(url)
        if role == "citizen":
            await page.fill("input[name=mobile]", mobile)
            before = _last_msg_id(mobile)
            async with page.expect_navigation():
                await page.click("button[type=submit]")
            otp = await asyncio.to_thread(read_otp, mobile, before)
            await page.fill("input[name=otp]", otp)
        else:
            await page.fill("input[name=username]", username)
            await page.fill("input[name=password]", password)
        async with page.expect_navigation():
            await page.click("button[type=submit]")
        seen["consent_page"] = (await page.inner_text("h2"))
        await page.click("button[value=allow]")
        await page.wait_for_timeout(800)

    async def on_callback():
        for _ in range(100):
            if _codes:
                qs = parse_qs(urlparse(_codes.pop()).query)
                return qs["code"][0], qs.get("state", [None])[0]
            await asyncio.sleep(0.2)
        raise TimeoutError("no OAuth callback")

    auth = OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(client_name="Standard MCP OAuth client (e2e)",
                                            redirect_uris=[AnyUrl("http://localhost:%d/callback" % CALLBACK_PORT)],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=on_redirect, callback_handler=on_callback)
    async with streamablehttp_client(server_url, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
    await browser.close(); await pw.stop()
    return storage, seen, tools


def claude_code(server_name, url, token, messages, workdir):
    with open(os.path.join(workdir, "mcp.json"), "w") as fh:
        json.dump({"mcpServers": {server_name: {"type": "http", "url": url,
                                                "headers": {"Authorization": "Bearer " + token}}}}, fh)
    sid, transcript = None, []
    for m in messages:
        a = assistant_turn(m, workdir, sid, server_name)
        sid = a["session_id"]
        transcript.append({"role": "person", "text": m})
        transcript.append({"role": "assistant", "text": a["all_text"], "tools": a["tools"], "error": a["error"]})
    return transcript


def tools_used(transcript):
    return [t["name"].split("__")[-1] for x in transcript if x["role"] == "assistant" for t in x.get("tools", [])
            if t["name"] != "ToolSearch"]


def main():
    threading.Thread(target=HTTPServer(("127.0.0.1", CALLBACK_PORT), _CB).serve_forever, daemon=True).start()
    started = datetime.now()
    mobile = fresh_mobile()
    work = tempfile.mkdtemp(prefix="e2e-")
    name, dob = "Sabitri Bora", "21/04/1951"
    make_age_proof(os.path.join(work, "sabitri_voter_id.jpg"), name, dob)
    seed = q("SELECT count(*) FROM applications WHERE submitted_at < '2026-10-01'")[0][0]
    step("Original database present", seed >= 100000, {"seeded_applications": seed})

    # 1. citizen sign-in with a standard client, discovery from the 401
    c_store, c_seen, c_tools = asyncio.run(standard_oauth(CITIZEN_MCP, "citizen", mobile=mobile))
    step("Citizen: standard OAuth client discovers and signs in",
         c_seen.get("scope_requested") == "citizen" and "submit_application" in c_tools,
         {"scope_requested": c_seen.get("scope_requested"), "resource": c_seen.get("resource"),
          "consent_page": c_seen.get("consent_page"), "tools_listed": c_tools})
    ctoken = c_store.tokens.access_token

    # 2. Claude Code (Haiku) applies
    tr1 = claude_code("sewasetu", CITIZEN_MCP, ctoken, [
        "Please apply for the old age pension for my mother. We signed in with her mobile.",
        "%s, born %s, female, widow, village Lakhipur, Sonari block. Bank account 302211004455, IFSC "
        "SBIN0003077. Her voter ID photo is sabitri_voter_id.jpg in this folder." % (name, dob),
        "Yes, all correct. She agrees to the declaration. Please submit.",
    ], work)
    row = q("SELECT application_no, status, applicant_name, to_char(dob,'DD/MM/YYYY'), doc_path FROM applications "
            "WHERE mobile = %s", (mobile,))
    app_no = row[0][0] if row else None
    audit = q("SELECT note FROM audit_log l JOIN applications a ON a.id = l.application_id WHERE a.mobile = %s "
              "AND l.action = 'SUBMIT'", (mobile,))
    step("Citizen: Claude Code (Haiku) chose the tools and applied", bool(row) and "submit_application" in tools_used(tr1),
         {"tools": tools_used(tr1)})
    step("Database: application in the original PostgreSQL", bool(row) and row[0][1] == "PENDING" and row[0][3] == dob
         and bool(row[0][4]) and audit and audit[0][0].startswith("AI assistant"),
         {"row": row[0][:4] if row else None, "audit_note": audit[0][0] if audit else None})

    # 4. officer sign-in with the standard client against the officer endpoint
    o_store, o_seen, o_tools = asyncio.run(standard_oauth(OFFICER_MCP, "officer", username=OFFICER[0], password=OFFICER[1]))
    step("Officer: standard OAuth client gets the officer sign-in (not the citizen OTP)",
         o_seen.get("scope_requested") == "officer" and "record_decision" in o_tools and "submit_application" not in o_tools,
         {"scope_requested": o_seen.get("scope_requested"), "resource": o_seen.get("resource"),
          "consent_page": o_seen.get("consent_page"), "tools_listed": o_tools})

    # 5. Claude Code (Haiku) as the officer approves
    tr2 = claude_code("sewasetu_officer", OFFICER_MCP, o_store.tokens.access_token, [
        "Find application %s and review it, including the age proof." % app_no,
        "Approve it. I have checked the voter ID against the form.",
    ], work)
    dec = q("SELECT status, decided_by FROM applications WHERE application_no = %s", (app_no,))
    note = q("SELECT note, actor FROM audit_log l JOIN applications a ON a.id = l.application_id WHERE "
             "a.application_no = %s AND l.action = 'APPROVED'", (app_no,))
    step("Officer: Claude Code (Haiku) reviewed and approved, against her name",
         dec == [("APPROVED", OFFICER[0])] and note and OFFICER[2] in note[0][0] and "record_decision" in tools_used(tr2),
         {"tools": tools_used(tr2), "db": dec, "audit": note[0] if note else None})

    # 6. citizen follows up, through the assistant and the status portal
    tr3 = claude_code("sewasetu", CITIZEN_MCP, ctoken, ["What is the status of my mother's pension application now?"], work)
    said = tr3[-1]["text"]
    results = " ".join(t.get("result", "") for x in tr3 if x["role"] == "assistant" for t in x.get("tools", []))
    step("Citizen: follow-up through the assistant shows the decision",
         "my_applications" in tools_used(tr3) and '"status": "APPROVED"' in results
         and re.search(r"\bapproved\b", said, re.I) and not re.search(r"\bis \**pending", said, re.I),
         {"tools": tools_used(tr3), "assistant_said": said[:300]})
    s = requests.Session()
    page = s.post(PORTAL + "/status", data={"mobile": mobile, "password": dob.replace("/", ""),
                                            "csrf_token": csrf(s, PORTAL + "/status")}).text
    step("Status portal (web) shows the same record", app_no in page and "APPROVED" in page,
         {"application_no_on_page": app_no in page, "status_on_page": "APPROVED" if "APPROVED" in page else None})

    # 7. sign-out ends the assistant's access at once
    requests.post(PORTAL + "/oauth/revoke", data={"token": c_store.tokens.refresh_token})
    r = requests.post(CITIZEN_MCP, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      headers={"Authorization": "Bearer " + ctoken, "Accept": "application/json, text/event-stream"})
    step("Sign-out: the next call is refused", r.status_code == 401, {"status": r.status_code})

    out = {"kind": "end-to-end", "ran_at": started.isoformat(timespec="seconds"),
           "client": "MCP Python SDK OAuthClientProvider (sign-in) + Claude Code %s, model haiku (conversations)" %
                     subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
           "application_no": app_no, "passed": sum(x["passed"] for x in STEPS), "total": len(STEPS), "steps": STEPS,
           "transcripts": {"citizen_apply": tr1, "officer_decide": tr2, "citizen_follow_up": tr3}}
    with open(os.path.join(HERE, "results", "e2e-latest.json"), "w", encoding="utf-8") as fh:
        json.dump(redact(out), fh, indent=1, ensure_ascii=False, default=str)
    print("\n%d/%d steps passed" % (out["passed"], out["total"]))


if __name__ == "__main__":
    main()
