# Design note: Sewa Setu assistant

**Goal.** Any AI assistant can help a citizen apply for the old age pension and follow it up, and
a block officer's assistant can work her queue. The portal stays the system of record.

## Decision 1: the portal is the only rulebook

The assistant is another counter window, not a second system. Both MCP servers import the portal
and call its own functions: `validate_application()`, `create_application()`, and a new
`decide_application()` that the web admin now uses too. No rule is restated in the MCP layer, so an
application made through an assistant appears on the status portal and the admin screens exactly
like a web application, and the reverse holds too.

I moved the guarantees that a caller could get wrong into the database:
- a partial unique index allowing one active application per mobile;
- a unique application number (random numbers would have collided about 6.5% of the time);
- a per-mobile lock.

Small models retry, so `submit_application` is idempotent. Six parallel submits still create
exactly one row.

The same rule now protects approvals. The officer's assistant cannot approve someone who was under
60 when she applied, and neither can the web form. The snapshot shows why this matters: 2,585
approvals went to people who were under 60 when they applied.

## Decision 2: identity comes from the sign-in, never from the conversation

Each MCP server is an OAuth resource server for one role, on its own endpoint, with its own token
audience and its own Protected Resource Metadata:
- the citizen's assistant cannot see officer tools, and a citizen token is refused at the officer
  endpoint;
- the applicant is whoever proved the mobile by OTP, and no tool accepts a mobile number as an
  argument;
- officers have named accounts with a block jurisdiction, so a decision is "recorded against her
  name";
- every call checks that the sign-in is still live, so on a shared phone or kiosk "end this
  sign-in" stops the assistant immediately;
- unattended program tokens can read but never decide, and can now be revoked.

Personal numbers are masked in what the model sees. The age-proof image goes only to the officer's
assistant, for comparing dates of birth.

## Decision 3: built for a small model with few turns, and measured that way

The evaluators use Claude Code with a small model and a turn limit, so the design follows from
that:
- 7 citizen tools and 4 officer tools;
- one call saves everything, accepting "15 Aug 1952", "sonari" or "widow";
- every result says what is missing, the next step, and a plain `say_to_applicant` message;
- the age proof goes up through a 30-minute link that opens on a phone or works with `curl`, so
  no photo has to travel through a chat;
- the officer gets evidence, not verdicts: the portal computes flags (under 60 on the day of
  application, possible duplicate beneficiary, shared bank account, invalid IFSC, deemed approval
  soon).

I tested this exactly as it will be tested: Claude Code running Haiku, signed in through the real
OAuth flow, talking to invented people played by another model, then reading the database. Those
runs changed the design:
- flags now name the block and the matching fields, because the officer's assistant stalled on a
  record outside her block;
- results carry a ready-to-say message, because Haiku once started speaking as the applicant;
- officer guidance now explains that there is no "hold": an undecided case is deemed approved.

## What I would do differently with more time

**I would have dealt with deemed approval before the first boot.** The portal's startup job
approves every pending case past its 15-day SLA. On the snapshot that is all 700 cases, including
9 applicants still under 60 and 2 who look like existing pensioners with a new mobile. I let it run
as shipped because it is the law and the portal's own rule, and the system of record is not mine to
edit quietly. With more time I would have built the officer's triage view first and taken one
question to the Director: should a case that fails the scheme rules be escalated instead of deemed
approved? I would also have asked which blocks the assistant pilot covers. That list is
configurable (`AGENT_BLOCKS`) and currently allows all six.
