"""REST identity and scopes (BUILD.md R5).

- Local request (loopback client, no tunnel headers, loopback Host) without a token = owner scope.
- Header `X-Prova-Actor: mcp` (set by prova/mcp_server.py) = an external agent: scope PROVA_MCP_SCOPE (default runtime).
- Tunnel or non-loopback requests need a bearer token: PROVA_TOKEN (owner) or PROVA_MCP_TOKEN (agent scope).
- A browser page from another origin cannot change anything (Origin check) and DNS rebinding is refused (Host check).
"""
import hmac
import os
import re
import secrets
from urllib.parse import urlparse

from prova.guard import registry

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver", "testclient"}
PUBLIC_PREFIXES = ("/k/", "/join", "/inbox/", "/api/inbox/", "/c/", "/t/")   # customer / owner links carry their own token
_RANK = {s: i for i, s in enumerate(registry.SCOPES)}

# (method, path regex) -> scope. First match wins. Anything mutating and not listed = owner only.
_ROUTES = [
    ("POST", r"/api/(reset|run_all|setup/sample|clock)$", "dev"),
    ("POST", r"/api/(setup/(approve|live)|ready)$", "owner"),
    ("DELETE", r"/api/customers/\d+$", "runtime"),
    ("POST", r"/api/confirm/", "owner"),
    ("POST", r"/api/(owner/turn_on|setup2/resume)$", "owner"),
    ("GET", r"/api/owner$", "owner"),
    ("GET", r"/api/confirmations$", "owner"),
    ("POST", r"/api/setup/(data|doc|url|policy|playbook|eval|pause)$", "setup"),
    ("POST", r"/api/setup2/", "setup"),
    ("PUT", r"/api/policy/", "setup"),
    ("POST", r"/api/(plan|goals)$", "setup"),
    ("POST", r"/api/lab/simulate$", "runtime"),
    ("POST", r"/api/(calls|campaign|followups/run|consent/send|morning_run)$", "runtime"),
    ("POST", r"/api/handoffs/\d+/resolve$", "runtime"),
]


def token() -> str | None:
    return os.environ.get("PROVA_TOKEN") or None


def ensure_token(say=print) -> str:
    t = token()
    if not t:
        t = secrets.token_urlsafe(24)
        os.environ["PROVA_TOKEN"] = t
        say(f"\n  PROVA_TOKEN (generated, needed for tunnel / non-local requests): {t}\n")
    return t


def check_bind(host: str) -> None:
    """Refuse a non-loopback bind unless PROVA_ALLOW_LAN=1 (then a token is generated and required)."""
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    if os.environ.get("PROVA_ALLOW_LAN") != "1":
        raise SystemExit(f"Refusing PROVA_HOST={host}: Prova binds to 127.0.0.1 only. Set PROVA_ALLOW_LAN=1 to override "
                         "(a bearer token is then required). Never do this on venue Wi-Fi.")
    ensure_token()


def required_scope(method: str, path: str) -> str:
    for m, rx, scope in _ROUTES:
        if m == method and re.match(rx, path):
            return scope
    if method in ("GET", "HEAD", "OPTIONS"):
        return "read"
    return "owner"


def _tunnel(h) -> bool:
    return "cf-ray" in h or "cf-connecting-ip" in h or h.get("host", "").endswith(".trycloudflare.com")


def _given(request) -> str | None:
    h = request.headers
    a = h.get("authorization", "")
    if a.lower().startswith("bearer "):
        return a[7:].strip()
    return h.get("x-prova-token") or request.cookies.get("prova_token")


def identify(request):
    """-> (actor, error). actor = {role, scope, session, name}; error = (status, rule, message) or None."""
    h = request.headers
    client = request.client.host if request.client else ""
    host = (h.get("host") or "").rsplit(":", 1)[0] if not (h.get("host") or "").startswith("[") else "[::1]"
    given = _given(request)
    owner_t, mcp_t = token(), os.environ.get("PROVA_MCP_TOKEN")
    if given and owner_t and hmac.compare_digest(given, owner_t):
        actor = {"role": "owner", "scope": "owner", "session": "owner-token", "name": "owner"}
    elif given and mcp_t and hmac.compare_digest(given, mcp_t):
        actor = {"role": "mcp", "scope": os.environ.get("PROVA_MCP_SCOPE", "runtime"), "session": "mcp-token", "name": h.get("x-prova-client", "mcp")}
    else:
        local = client in LOOPBACK and not _tunnel(h)
        if not local:
            return None, (401, "token_required", "A bearer token is required for tunnel and non-local requests.")
        if host.lower() not in LOOPBACK_HOSTS:
            return None, (403, "bad_host", "Unexpected Host header.")
        if h.get("x-prova-actor") == "mcp":
            actor = {"role": "mcp", "scope": os.environ.get("PROVA_MCP_SCOPE", "runtime"), "session": "mcp-local",
                     "name": h.get("x-prova-client", "mcp")}
        else:
            actor = {"role": "owner", "scope": "owner", "session": "owner-local", "name": "owner"}
            origin = h.get("origin")
            if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
                oh = urlparse(origin).hostname or ""
                if oh not in LOOPBACK_HOSTS and oh not in LOOPBACK:
                    return None, (403, "bad_origin", "Cross-origin write refused.")
    if actor["scope"] not in _RANK:
        actor["scope"] = "read"
    return actor, None


def scope_error(actor: dict, method: str, path: str):
    """-> (status, rule, message) when the actor's scope is too low for this route, else None."""
    need = required_scope(method, path)
    if need == "dev":
        if actor["role"] == "owner" or os.environ.get("PROVA_DEV") == "1":
            return None
        return 403, "scope", "This wipes or rewrites the desk; it is available to an agent only when the server runs with PROVA_DEV=1."
    if _RANK[actor["scope"]] < _RANK[need]:
        return 403, "scope", f"Needs {need} scope; this client has {actor['scope']}. Ask the owner."
    return None
