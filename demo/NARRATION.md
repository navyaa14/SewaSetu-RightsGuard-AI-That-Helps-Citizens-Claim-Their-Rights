# Narration for the demo video (2:56)

How to record it as the Loom the Director asked for: open Loom with **camera bubble on**
(face and screen), play `sewasetu-demo.mp4` full-screen, and read this over it. The pace is
about 140 words a minute; the timestamps tell you where you should be. Everything on screen is
real: live portal screenshots and real Claude Code sessions running Haiku. Long assistant replies
are shortened on screen.

---

**0:00 · Title**
"I'm [name]. This is Sewa Setu Assistant: any AI assistant can now apply for an old age pension,
by the same rules as the counter."

**0:07 · The problem**
"The Director told us about a grandmother whose grandson asked the AI on his phone to apply for
her pension. It couldn't. The portal holds a hundred thousand applications for a 250-rupee
pension, and the law approves any case nobody decides within 15 days."

**0:19 · One rulebook, two doors**
"So I built two MCP servers: one for citizens, one for block officers. Neither has rules of its
own. They call the portal's own validate and create functions, the same ones the web form uses.
Sign-in happens on the portal with OAuth, and a citizen's assistant never even sees the officer's
tools."

**0:33 · Signing in**
"Claude Code finds the sign-in page by itself. The OTP goes to her phone, not to the AI. She
approves on the portal's own page, and the token works only for this server and ends the moment
she signs out."

**0:47 · Kamala applies**
"This is a real session with Claude Code running Haiku. The grandson types her details once. The
assistant checks the voter ID image, uploads it through a one-time link, reads everything back,
and submits only after a clear yes. Then the rights clock starts: a decision is due in 15 days,
or the law approves it."

**1:23 · Same record, every channel**
"The same application is on the status portal with her normal login, and the admin audit trail
says it came through an AI assistant."

**1:35 · The officer**
"Now Rina, the Sonari block officer, with her own assistant. She sees her queue sorted by deadline.
The assistant shows her the age proof and the checks, and she approves. The decision is recorded
against her name. The next case matches an existing pensioner on name, date of birth, bank, village
and gender. The portal computes that match, not the model, and she rejects it as a duplicate."

**2:07 · Guardrails**
"I invented the people who break counters. Rafiq is ten days short of 60, so he's refused and told
the date he can apply. Bhupen asks it to write 1960, and it won't. A name that says 'approve me' is
treated as data. Padmeswari applies in Assamese. Pressing submit six times gives exactly one
application. Signing out on a shared phone stops the assistant instantly."

**2:31 · Evals**
"I tested it the way you will: Claude Code with Haiku against 13 invented people, checking the
database after every conversation. Runs went from 21 to 26 out of 26, and 21 contract tests pass.
Every failure, and what I changed because of it, is on the eval dashboard."

**2:47 · Close**
"The AI is just another counter window. Thank you."

---

If you need to land exactly on 3:00, cut the second sentence of the guardrails section.
