"""Single-instance Render entrypoint: portal, MCP, local SMS and daily scheduler."""
import asyncio
import contextlib
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("TZ", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()
url = urlparse(os.environ["DATABASE_URL"])
for key, value in {
    "DB_HOST": url.hostname, "DB_PORT": str(url.port or 5432),
    "DB_USER": unquote(url.username or ""), "DB_PASSWORD": unquote(url.password or ""),
    "DB_NAME": unquote(url.path.lstrip("/")),
}.items():
    os.environ[key] = value
os.environ.setdefault("PGSSLMODE", "require")
port = int(os.environ.get("PORT", "10000"))
base = os.environ.get("PUBLIC_BASE_URL") or os.environ["RENDER_EXTERNAL_URL"]
os.environ["PUBLIC_BASE_URL"] = base.rstrip("/")
os.environ["CITIZEN_MCP_URL"] = base.rstrip("/") + "/citizen/mcp"
os.environ["OFFICER_MCP_URL"] = base.rstrip("/") + "/officer/mcp"
os.environ["PORTAL_INTERNAL_URL"] = "http://127.0.0.1:%d" % port
os.environ["SMS_GATEWAY_URL"] = "http://127.0.0.1:8025"
os.environ.setdefault("UPLOAD_DIR", "/tmp/sewasetu-uploads")
os.environ["EVAL_RESULTS_DIR"] = str(ROOT / "eval" / "results")
os.environ.setdefault("GIT_COMMIT", os.environ.get("RENDER_GIT_COMMIT", "unknown"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("render")


def initialize_database():
    import psycopg2
    # Schema, seed and completion marker commit atomically. Never reload the seed
    # over existing applications, including after a restart or interrupted deploy.
    with contextlib.closing(psycopg2.connect(os.environ["DATABASE_URL"])) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(424243)")
            cur.execute((ROOT / "app/schema.sql").read_text())
            cur.execute("CREATE TABLE IF NOT EXISTS render_bootstrap (id integer PRIMARY KEY)")
            cur.execute("SELECT 1 FROM render_bootstrap WHERE id=1")
            if not cur.fetchone():
                cur.execute("SELECT count(*) FROM applications")
                if cur.fetchone()[0] == 0:
                    with (ROOT / "seed/seed.sql").open() as seed:
                        for line in seed:
                            if line.startswith("COPY "):
                                data = io.StringIO()
                                for row in seed:
                                    if row.rstrip("\r\n") == "\\.":
                                        break
                                    data.write(row)
                                data.seek(0)
                                cur.copy_expert(line.strip(), data)
                            elif line.startswith("SELECT "):
                                cur.execute(line)
                    log.info("Loaded bundled seed")
                cur.execute("INSERT INTO render_bootstrap VALUES (1)")
    subprocess.run([sys.executable, str(ROOT / "app/migrate.py")], check=True)


def configure_upload_storage(portal):
    # Free web services have ephemeral filesystems. Preserve validated documents
    # in Postgres and restore the local cache on every boot.
    upload_dir = Path(portal.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(portal.get_db()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS render_documents "
                        "(name text PRIMARY KEY, content bytea NOT NULL)")
            cur.execute("SELECT name, content FROM render_documents")
            while rows := cur.fetchmany(20):
                for name, content in rows:
                    if Path(name).name == name:
                        (upload_dir / name).write_bytes(bytes(content))
    original = portal.store_age_proof

    def store(mobile, filename, content):
        path, problem = original(mobile, filename, content)
        if problem:
            return path, problem
        try:
            with contextlib.closing(portal.get_db()) as conn, conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO render_documents (name,content) VALUES (%s,%s)",
                                (Path(path).name, content))
        except Exception:
            Path(path).unlink(missing_ok=True)
            log.exception("Document persistence failed")
            return None, "Document storage is unavailable. Please try again."
        return path, None

    portal.store_age_proof = store


async def scheduler():
    from scripts.deemed_approval import main
    while True:
        try:
            await asyncio.to_thread(main)
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Approval job failed; retrying in five minutes")
            await asyncio.sleep(300)


def create_app():
    import app as portal
    from mcp_servers import citizen, officer
    from starlette.applications import Starlette
    from starlette.middleware.wsgi import WSGIMiddleware

    configure_upload_storage(portal)
    portal_asgi = WSGIMiddleware(portal.app)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        sms = subprocess.Popen([
            sys.executable, "-m", "gunicorn", "--bind", "127.0.0.1:8025",
            "--workers", "1", "--threads", "4", "--access-logfile", "-", "app:app",
        ], cwd=ROOT / "smsgw")
        task = None
        try:
            import requests
            for _ in range(50):
                if sms.poll() is not None:
                    raise RuntimeError("SMS simulator exited during startup")
                try:
                    await asyncio.to_thread(requests.get, "http://127.0.0.1:8025/", timeout=1)
                    break
                except requests.RequestException:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("SMS simulator did not become ready")
            async with contextlib.AsyncExitStack() as stack:
                for endpoint in (citizen.app, officer.app):
                    await stack.enter_async_context(endpoint.inner.router.lifespan_context(endpoint.inner))
                task = asyncio.create_task(scheduler())
                yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            sms.terminate()
            try:
                await asyncio.to_thread(sms.wait, timeout=10)
            except subprocess.TimeoutExpired:
                sms.kill()
                await asyncio.to_thread(sms.wait)

    lifecycle = Starlette(lifespan=lifespan)

    async def dispatch(scope, receive, send):
        if scope["type"] == "lifespan":
            return await lifecycle(scope, receive, send)
        path = scope.get("path", "")
        for role, endpoint in (("citizen", citizen.app), ("officer", officer.app)):
            if path.startswith("/" + role + "/") or path.startswith(
                    "/.well-known/oauth-protected-resource/" + role + "/"):
                return await endpoint(scope, receive, send)
        return await portal_asgi(scope, receive, send)

    return dispatch


if __name__ == "__main__":
    initialize_database()
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=port, proxy_headers=True,
                forwarded_allow_ips="*", log_level="info")
