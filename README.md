# Sewa Setu Assistant: AI assistants that can apply, follow up and decide

Sewa Setu is the Old Age Pension portal of the (fictional) Government of Purvanchal. This repo
adds **two MCP servers** to it, so any AI assistant can help a citizen apply for her pension and
follow it up, and a block officer's assistant can work her queue. The portal stays the system of
record. Every rule, validation and write goes through the same functions the web form uses.

| | URL (after deploy) | Who signs in | How |
|---|---|---|---|
| Portal | `https://DOMAIN/` | everyone | the existing web pages |
| Citizen MCP | `https://DOMAIN/citizen/mcp` | the applicant | OAuth: mobile + OTP on the portal |
| Officer MCP | `https://DOMAIN/officer/mcp` | block officer | OAuth: departmental login on the portal |
| Eval dashboard | `https://DOMAIN/eval-dashboard` | public | |

The original handout is the first commit. Everything since is in the history.

## Connect an assistant

```bash
claude mcp add --transport http sewasetu        https://DOMAIN/citizen/mcp
claude mcp add --transport http sewasetu-officer https://DOMAIN/officer/mcp
```

The first tool call returns `401` with `WWW-Authenticate: Bearer resource_metadata=…, scope=…`.
The client reads the server's Protected Resource Metadata (RFC 9728), finds the portal as the
authorization server, registers itself (RFC 7591), and opens the portal's sign-in page. Citizens
enter their mobile number and the OTP (in this simulation the OTP shows on
`https://DOMAIN/__gateway/`). Officers use their departmental login. The person approves on the
consent page, and the assistant gets a token bound to that one server (RFC 8707 audience).

## What the assistant can do

**Citizen server.** Acts only for the mobile number that signed in. The mobile is taken from the
token and never from tool arguments.

| Tool | Purpose |
|---|---|
| `scheme_info` | Live facts from the portal: amount, age rule, documents, deadline countdown, blocks served. This fixes the "outdated information" problem. |
| `check_eligibility` | Checks a date of birth and block without saving. If under 60, gives the exact date she turns 60 and whether that falls before the window closes. |
| `save_application_details` | Takes everything in one call and normalises it ("15 Aug 1952", "sonari", "widow"). Runs the portal's `validate_application()` and reports what is missing or wrong in plain words. Returns an upload link. Drafts live on the server, so they survive the 2-hour sign-in limit. |
| `get_age_proof_upload_link` | A 30-minute link tied to her mobile. Opens on a phone, or works with `curl -F document=@file`. |
| `submit_application` | Needs `confirmed=true` after a read-back. Calls `validate_application()` and then `create_application()`. Idempotent: retries and parallel calls create exactly one application. |
| `my_applications` | Status in plain words, plus a **rights clock**: the decision-due date, the deemed-approval rule, and the reason if rejected. |
| `withdraw_application` | Pending applications only. The application number must be given twice. |

**Officer server.** Jurisdiction comes from the officer's account (one block, or all blocks for
the district login).

| Tool | Purpose |
|---|---|
| `pending_queue` | Cases closest to automatic deemed approval come first, with flags. |
| `find_application` | Search by application number, mobile, or name. |
| `review_application` | Masked personal numbers, the rule checks, flags, the audit trail, and the **age-proof image** so the officer can compare the date of birth. |
| `record_decision` | `APPROVED` or `REJECTED`, with a reason code, recorded against the officer's own name. Approval is refused when the scheme rules fail. Approving a case with warnings needs a note. |

Flags (computed by the portal and shown on the web admin page too): `UNDER_60_AT_SUBMISSION`,
`NO_AGE_PROOF`, `POSSIBLE_DUPLICATE_BENEFICIARY`, `SAME_BANK_ACCOUNT`, `IFSC_INVALID`,
`DEEMED_APPROVAL_SOON`, `VIA_ASSISTANT`.

## What changed in the portal, and why

Each fix came from reading the code and the 100k-row snapshot.

- **Session forgery.** `app.ini` ships a public `secret_key`, and compose didn't override it.
  Anyone could sign a session cookie with `admin=True`. The app now refuses to start without its
  own `SECRET_KEY`.
- **Duplicate applications under retries.** The one-active-application check was check-then-insert.
  A partial unique index (`uq_active_application_per_mobile`) plus a per-mobile advisory lock now
  enforce it. The seed has no violations, so the index applies cleanly.
- **Application-number collisions.** The numbers are random: 58,561 of 900,000 `SSP26` numbers are
  already used, so about 6.5% of new applications would reuse an existing number. There is now a
  unique index and a retry.
- **Uploads.** Files used the client's filename (path traversal and overwrites). They now get a
  server-chosen name and are checked by content.
- **Named block officers.** One shared `admin` login can't record a decision "against her name".
  The new `officers` table has a block jurisdiction. The env admin remains the district login.
- **One decision path.** `decide_application()` serves both the web form and the assistant:
  rejection reason codes (the four reasons already in the audit log, plus `UNDER_AGE` and `OTHER`),
  approval refused when the scheme rules fail, and the applicant gets an SMS on both channels.
- **OAuth.** Officers sign in with their own accounts. If a client omits the scope, it is inferred
  from the `resource`, so the officer endpoint never shows the citizen OTP page. Program tokens can
  now be revoked (they could not before). The server no longer fetches `http://localhost`
  client-metadata URLs (SSRF).
- **Immediate sign-out.** Every MCP call checks that the sign-in is still live. "End this sign-in"
  on the status portal therefore stops the assistant at once, not 20 minutes later. This matters on
  shared phones and kiosks.
- **Time zone.** The seed stores IST wall-clock times, but new rows were written in UTC. Containers
  now run in `Asia/Kolkata`. The deemed-approval job uses IST and audits exactly the rows it changed
  (it used to double-log when retried).
- **Pilot blocks.** `AGENT_BLOCKS` is the set of blocks served through assistants, following the
  Director's note. Applicants from other blocks are pointed to the web form or the Block Office.
  The rules are otherwise identical.

Not changed on purpose: the deemed-approval law itself, the scheme deadline (31 Oct 2026, 23:59
IST), and the status-portal password scheme (DOB as DDMMYYYY, set by a department circular). These
are policy decisions, listed in `DESIGN-NOTE.md`.

## Deploy (a VM with Docker)

```bash
git clone <this repo> && cd sewasetu-assistant
cp .env.example .env        # DOMAIN, SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD, OFFICER_ACCOUNTS
docker compose up -d --build
```

Point an A record at the VM (or use `<ip>.sslip.io`) and open ports 80 and 443. Caddy gets the
certificate. The first boot loads the seed (about a minute), runs the migrations, and runs the
deemed-approval job, which approves the 700 seeded pending applications because their 15-day SLA
has passed. That is the portal's own rule, applied as shipped.

## Run the evals

```bash
pip install -r app/requirements.txt
export PUBLIC_BASE_URL=https://DOMAIN CITIZEN_MCP_URL=https://DOMAIN/citizen/mcp \
       OFFICER_MCP_URL=https://DOMAIN/officer/mcp ADMIN_PASSWORD=... DB_HOST=...   # DB access for the checks
python eval/run_contract_tests.py                 # scripted MCP client + DB assertions
python eval/run_agent_evals.py --attempts 2       # Claude Code (Haiku) vs invented people
```

Results go to `eval/results/`, which the dashboard reads. See `eval/personas.py` for the people
and why each one exists.

## Layout

```
app/                 portal (Flask) + mcp_servers/ (citizen.py, officer.py, common.py)
app/migrations/      idempotent SQL applied at start (migrate.py)
eval/                personas, agent-eval runner, contract tests, results/
deploy/Caddyfile     TLS + routing
DESIGN-NOTE.md       the three decisions that mattered most
```
