# Render deployment

The `render.yaml` service runs the portal, both OAuth-protected MCP endpoints,
the local simulated SMS gateway and the approval scheduler in one instance.
It uses the managed database already created in Singapore:
`sewasetu-rightsguard-db` (`dpg-db9276avcj2c73bku900-a`).

1. Push `render.yaml` and `deploy/render_server.py` to the repository.
2. Create a Render Blueprint from this repository.
3. Set `DATABASE_URL` to that database's **Internal Database URL**, available
   under Connections on its Render dashboard. Keep this URL out of Git.
4. Apply the Blueprint. `SECRET_KEY` and `ADMIN_PASSWORD` are generated.
5. Read `ADMIN_PASSWORD` in the web service's Environment settings; the login
   is `admin`. Keep this password private.

Startup imports the bundled seed only into an empty database, in one
transaction, then applies existing migrations. Existing data is preserved.
Validated uploaded documents are stored in PostgreSQL and restored locally
after each restart. The SMS simulator stays on loopback and is accessed
through the existing `/__gateway/` route. It is a demo inbox, not real SMS.

The public URL is discovered from `RENDER_EXTERNAL_URL`. Endpoints:

- `/` portal
- `/citizen/mcp` citizen MCP
- `/officer/mcp` officer MCP
- `/healthz` database health
- `/eval-dashboard` existing recorded evaluations

The approval job runs at boot and daily at 02:00 IST while the instance is
awake. Free Render web instances sleep when idle, so missed runs catch up on
the next wake. For an always-on SLA, upgrade the web service or provision an
independent cron service. The free database expires on 15 November 2026.
This configuration is a hackathon demo, not a production citizen-data system.
