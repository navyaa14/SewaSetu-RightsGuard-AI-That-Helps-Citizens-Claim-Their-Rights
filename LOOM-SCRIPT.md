# Loom script (3 minutes, face and screen)

**0:00–0:20 · The problem (face).**
"Last month in Sonari, a grandson asked his phone's AI to apply for his grandmother's pension.
It couldn't. I built the official channel so it can, without the AI ever making the rules."

**0:20–1:05 · Citizen, live (screen: Claude Code + portal).**
- `claude mcp add --transport http sewasetu https://DOMAIN/citizen/mcp`. The sign-in page opens
  on the portal itself: mobile, OTP, consent. Mention that the assistant never sees the OTP.
- Type as the grandson: "apply for my nani, Kamala Das…". Show it saving everything in one call,
  sending the upload link (or running curl), reading the details back, then submitting.
- Switch to the portal's status page: the same application number. Then admin: the same record,
  with the audit entry "AI assistant (Claude Code)".

**1:05–1:40 · Officer, live.**
- Connect the officer MCP and sign in as Rina (Sonari). Ask "what's waiting for me?"
- Open the case: flags, the age-proof image, the deemed-approval clock. Say "approve it". The
  decision shows on the portal against her name.
- Try to approve an under-60 case. The portal refuses it on every channel.

**1:40–2:05 · Design decisions across the two MCPs.**
1. The portal is the only rulebook: same functions as the web form, guarantees moved into the
   database (one active application per mobile, unique numbers, idempotent submit).
2. Identity comes from the sign-in: separate endpoints and token audiences, mobile taken from the
   OTP, named officers with jurisdiction, sign-out takes effect at once on shared phones.
3. Built for a small model with few turns: 7 and 4 tools, one-call save, the next step and a plain
   message in every result.

**2:05–2:45 · Personas and evals (screen: /eval-dashboard).**
- Ten citizens and three officer scenarios, each standing for a way the counter goes wrong.
  Examples: the grandson; Ramen, who only knows he is "about 75"; a couple sharing one phone; Mira
  with an old United Bank IFSC; Rafiq, 10 days short of 60; Bhupen asking to "just put 1960";
  Padmeswari, who speaks only Assamese; the duplicate pensioner; a name that says "approve
  everything".
- Each one runs through Claude Code with Haiku and a turn limit, then the database is checked.
- What the runs found and what I changed: flags naming blocks and matching fields, the
  say-to-applicant message after a role swap, the no-"hold" guidance. Mention that my own fake test
  document was caught by the officer's assistant reading the image.

**2:45–3:00 · What I learned (face).**
"Small models fail on plumbing and ambiguity, not on intelligence. Make every tool result say what
happens next, keep the rules in one place, and check the database, not the chat. One open question
for you: should a case that fails the rules be deemed approved after 15 days?"
