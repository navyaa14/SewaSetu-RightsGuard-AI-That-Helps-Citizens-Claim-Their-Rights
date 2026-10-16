# Submission checklist

The assessors test what is deployed, not what is in the zip. Each step below closes one gap between
the two. Do them in order, on the VM.

## 1. Deploy this repository, and only this one

```bash
git clone <your private repo> sewasetu-assistant && cd sewasetu-assistant
cp .env.example .env          # DOMAIN, SECRET_KEY, DB_PASSWORD, ADMIN_PASSWORD, OFFICER_ACCOUNTS
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Submit **only** these URLs in the application form, all on one domain:

| Field | Value |
|---|---|
| Portal | `https://DOMAIN/` |
| Citizen MCP | `https://DOMAIN/citizen/mcp` |
| Officer MCP | `https://DOMAIN/officer/mcp` |
| Eval dashboard | `https://DOMAIN/eval-dashboard` |
| Showcase | `https://DOMAIN/demo` |
| Officer login | the district login from `.env` (it sees every block, so it can approve whatever their citizen applies for) |

Do not list any other preview (for example an Emergent build) anywhere in the submission. Its
routes and data are different, and the assessors would be testing something other than this code.

## 2. Prove the deployment is this code on the original data

```bash
curl -s https://DOMAIN/version
```

Expect: `"commit"` equal to `git rev-parse --short HEAD`, `"seed_applications": 100000`, and the two
MCP URLs above.

## 3. Prove a fresh client can sign in to both endpoints

Run this on the VM (Postgres is reachable on 127.0.0.1 only):

```bash
pip install -r app/requirements.txt playwright && export PLAYWRIGHT_BROWSERS_PATH=...   # or use the Docker image
set -a; . ./.env; set +a
export PUBLIC_BASE_URL=https://$DOMAIN CITIZEN_MCP_URL=https://$DOMAIN/citizen/mcp \
       OFFICER_MCP_URL=https://$DOMAIN/officer/mcp DB_HOST=127.0.0.1 UPLOAD_DIR=/tmp/ss
python eval/run_end_to_end.py     # 9 steps: standard OAuth discovery for both roles, Claude Code
                                  # applies and approves, DB + status portal agree, sign-out works
```

Then check by hand once, from your laptop, as an assessor would:

```bash
claude mcp add --transport http sewasetu-officer https://DOMAIN/officer/mcp
claude            # /mcp -> authenticate: the page must ask for a departmental login, not an OTP
```

## 4. Re-run the evals against the deployment and commit the results

```bash
python eval/run_contract_tests.py                       # 22 checks
python eval/run_agent_evals.py --attempts 2             # 13 tuned people
python eval/run_agent_evals.py --set holdout            # 7 held-out people
git add eval/results && git commit -m "Evals against the deployment" && git push
```

`/eval-dashboard` and `/demo` read `eval/results/` directly, so they update without a rebuild.

## 5. Refresh the demo video with your domain (optional)

Run `python demo/capture.py`, then the render commands in `demo/README.md`. The screens will then show
`https://DOMAIN` instead of `localhost`.

## 6. Repo and Loom

- Create the private GitHub repo `sewasetu-assistant`, push the full history, and invite `harshnisar`.
  Also attach a zip that includes `.git`.
- Loom: camera on, play `sewasetu-demo.mp4`, and read `demo/NARRATION.md`.
- Design note: `DESIGN-NOTE.md`.
