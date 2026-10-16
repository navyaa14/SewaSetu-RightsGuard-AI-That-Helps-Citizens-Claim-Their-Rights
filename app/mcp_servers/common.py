# -*- coding: utf-8 -*-
"""Shared plumbing for the two Sewa Setu MCP servers.

Each server is an OAuth 2.1 *resource server* (MCP authorization spec):
  - it publishes Protected Resource Metadata (RFC 9728) naming the portal as its
    authorization server and the one scope it accepts;
  - unauthenticated calls get 401 + WWW-Authenticate pointing at that metadata, so a
    client such as Claude Code can discover where and how to sign the person in;
  - it verifies the portal's RS256 JWTs with the portal's JWKS, checks `iss`, checks that
    `aud` is *this* server (RFC 8707), and checks role + scope;
  - it checks the sign-in is still live in the portal, so "end this sign-in" on the status
    portal (shared phones, kiosks) stops the assistant immediately rather than in 20 min.

The portal module itself (`app`) is imported so that every rule, validation and write
goes through the same functions the web form uses. The portal stays the system of record.
"""
import os
import sys
import json
import time
import logging
import contextvars
from datetime import datetime

import jwt
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as portal  # noqa: E402  (the Flask portal: rules, DB, audit)

log = logging.getLogger("sewasetu.mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PORTAL_INTERNAL_URL = os.environ.get("PORTAL_INTERNAL_URL", ISSUER).rstrip("/")

current_auth = contextvars.ContextVar("sewasetu_auth", default=None)


class AuthInfo(dict):
    """Verified claims plus derived identity."""

    @property
    def subject(self):
        return self["sub"]

    @property
    def principal(self):  # mobile number or officer username
        return self["sub"].split(":", 1)[1]

    @property
    def is_program(self):
        return self.get("kind") == "program"


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------

_JWKS = {"keys": {}, "fetched": 0.0}


def _jwks_key(kid):
    if kid not in _JWKS["keys"] or time.time() - _JWKS["fetched"] > 3600:
        try:
            doc = requests.get(PORTAL_INTERNAL_URL + "/.well-known/jwks.json", timeout=5).json()
            _JWKS["keys"] = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
                             for k in doc.get("keys", [])}
            _JWKS["fetched"] = time.time()
        except Exception as e:  # portal unreachable: keep old keys
            log.warning("jwks fetch failed: %s", e)
    return _JWKS["keys"].get(kid)


def _audiences(public_url):
    u = public_url.rstrip("/")
    base = u[:-4] if u.endswith("/mcp") else u
    return {u, u + "/", base, base + "/", base + "/mcp", base + "/mcp/"}


def _session_live(claims):
    """Is the sign-in behind an access token still live (not ended by the person)?"""
    conn = portal.get_db()
    cur = conn.cursor()
    try:
        if claims.get("kind") == "program":
            cur.execute("SELECT revoked_at, expires_at FROM oauth_program_tokens WHERE jti = %s",
                        (claims.get("jti"),))
            row = cur.fetchone()
            return bool(row) and row[0] is None and row[1] > datetime.now()
        cur.execute("SELECT expires_at FROM oauth_refresh_tokens WHERE subject = %s AND client_id = %s "
                    "AND revoked_at IS NULL AND expires_at > %s",
                    (claims["sub"], claims.get("client_id"), datetime.now()))
        ends = claims.get("session_ends")
        for (exp,) in cur.fetchall():
            if ends is None or abs(int(exp.timestamp()) - int(ends)) <= 5:
                return True
        return False
    finally:
        cur.close(); conn.close()


def verify_bearer(token, public_url, role):
    """Returns (AuthInfo, None) or (None, (status, error, description))."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None, (401, "invalid_token", "malformed token")
    key = _jwks_key(header.get("kid"))
    if key is None:
        return None, (401, "invalid_token", "unknown signing key")
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER,
                            audience=list(_audiences(public_url)),
                            options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.ExpiredSignatureError:
        return None, (401, "invalid_token", "token expired")
    except jwt.InvalidAudienceError:
        return None, (401, "invalid_token", "token was issued for a different service")
    except jwt.PyJWTError as e:
        return None, (401, "invalid_token", "token rejected: %s" % e.__class__.__name__)
    if claims.get("role") != role or claims.get("scope") != role or \
            not str(claims.get("sub", "")).startswith(role + ":"):
        return None, (403, "insufficient_scope",
                      "this endpoint is for the %s role; sign in as %s" % (role, role))
    if claims.get("kind") not in ("access", "program"):
        return None, (401, "invalid_token", "not an access token")
    if claims.get("kind") == "program" and role != "officer":
        return None, (403, "insufficient_scope", "program tokens are for officer services only")
    if not _session_live(claims):
        return None, (401, "invalid_token", "this sign-in has ended; sign in again")
    return AuthInfo(claims), None


# ---------------------------------------------------------------------------
# ASGI app: protected resource metadata + auth gate in front of FastMCP
# ---------------------------------------------------------------------------

def prm_document(public_url, role, name):
    return {
        "resource": public_url,
        "authorization_servers": [ISSUER],
        "scopes_supported": [role],
        "bearer_methods_supported": ["header"],
        "resource_name": name,
        "resource_documentation": ISSUER + "/about",
    }


class AuthGate:
    """Pure ASGI middleware (does not buffer streamed responses)."""

    def __init__(self, app, public_url, role, name):
        self.app, self.public_url, self.role, self.name = app, public_url.rstrip("/"), role, name
        from urllib.parse import urlparse
        path = urlparse(self.public_url).path.rstrip("/")
        self.mcp_path = path or "/mcp"
        self.prm_url = self._prm_url()

    def _prm_url(self):
        from urllib.parse import urlparse
        u = urlparse(self.public_url)
        return "%s://%s/.well-known/oauth-protected-resource%s" % (u.scheme, u.netloc, u.path.rstrip("/"))

    async def _send_json(self, send, status, body, headers=()):
        data = json.dumps(body).encode()
        hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                (b"access-control-allow-origin", b"*"),
                (b"access-control-expose-headers", b"WWW-Authenticate, Mcp-Session-Id")]
        hdrs += [(k.encode(), v.encode()) for k, v in headers]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path.startswith("/.well-known/oauth-protected-resource"):
            return await self._send_json(send, 200, prm_document(self.public_url, self.role, self.name))
        if path.startswith("/.well-known/oauth-authorization-server"):
            # some clients look here on the resource host; point them at the portal's document
            try:
                doc = requests.get(PORTAL_INTERNAL_URL + "/.well-known/oauth-authorization-server", timeout=5).json()
                return await self._send_json(send, 200, doc)
            except Exception:
                return await self._send_json(send, 404, {"error": "not_found"})
        if path in ("/", "/healthz"):
            return await self._send_json(send, 200, {
                "ok": True, "service": self.name, "mcp_endpoint": self.public_url,
                "authorization_server": ISSUER, "scope": self.role})
        if method == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", b"*"), (b"access-control-allow-headers", b"*"),
                (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS")]})
            return await send({"type": "http.response.body", "body": b""})
        if path.rstrip("/") == self.mcp_path.rstrip("/") or path.startswith(self.mcp_path + "/"):
            auth = None
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    auth = v.decode("latin-1")
            challenge = 'Bearer resource_metadata="%s", scope="%s"' % (self.prm_url, self.role)
            if not auth or not auth.lower().startswith("bearer "):
                return await self._send_json(send, 401, {
                    "error": "unauthorized",
                    "error_description": "Sign in through the Sewa Setu portal (OAuth). Metadata: " + self.prm_url},
                    [("WWW-Authenticate", challenge)])
            import anyio
            info, err = await anyio.to_thread.run_sync(verify_bearer, auth[7:].strip(),
                                                       self.public_url, self.role)
            if err:
                status, code, desc = err
                return await self._send_json(send, status, {"error": code, "error_description": desc}, [
                    ("WWW-Authenticate", '%s, error="%s", error_description="%s"' % (challenge, code, desc))])
            scope.setdefault("state", {})["auth"] = info
            token = current_auth.set(info)
            try:
                return await self.app(scope, receive, send)
            finally:
                current_auth.reset(token)
        return await self._send_json(send, 404, {"error": "not_found"})


def build_asgi(mcp, public_url, role, name):
    from mcp.server.transport_security import TransportSecuritySettings
    # Host-header checks are for servers bound to localhost; this one sits behind a TLS proxy
    # on a public name and authenticates every request, so the check is disabled.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    inner = mcp.streamable_http_app()
    return _Lifespan(AuthGate(inner, public_url, role, name), inner)


class _Lifespan:
    """Forward lifespan events to FastMCP's app (it starts the session manager)."""

    def __init__(self, gated, inner):
        self.gated, self.inner = gated, inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.inner(scope, receive, send)
        return await self.gated(scope, receive, send)


def get_auth(ctx=None):
    """The verified identity for the current tool call."""
    info = current_auth.get()
    if info is None and ctx is not None:
        try:
            info = ctx.request_context.request.state.auth
        except Exception:
            info = None
    if info is None:
        raise PermissionError("not signed in")
    return info


def mask_mobile(m):
    return ("XXXXXX" + m[-4:]) if m and len(m) >= 4 else m


def mask_account(a):
    return ("X" * max(0, len(a) - 4) + a[-4:]) if a and len(a) > 4 else a


def iso(d):
    return d.isoformat(sep=" ", timespec="minutes") if d else None


def fmt_date(d):
    return d.strftime("%d %b %Y") if d else None
