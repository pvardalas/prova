"""Owner confirmation tokens for irreversible actions asked by an agent (erase customer, ...).

create() stores a pending action; only a human at the owner scope can consume() it, once, before it expires.
"""
import json
import secrets
from datetime import timedelta

from prova import db

TTL_MIN = 15


def _val(r):
    v = r["value"]
    return json.loads(v) if isinstance(v, str) else v


def create(action: str, args: dict, by: str = "agent") -> dict:
    db.init()
    token = secrets.token_urlsafe(16)
    exp = (db.now() + timedelta(minutes=TTL_MIN)).isoformat(timespec="seconds")
    db.q("INSERT INTO settings (key, value) VALUES (?, ?)",
         (f"confirm:{token}", json.dumps({"action": action, "args": args, "by": by, "exp": exp}, ensure_ascii=False)))
    return {"token": token, "expires": exp, "action": action, "args": args}


def pending() -> list:
    out = []
    for r in db.q("SELECT key, value FROM settings WHERE key LIKE 'confirm:%'"):
        v = _val(r)
        if v["exp"] >= db.now_iso():
            out.append({"token": r["key"][8:], **v})
    return out


def consume(token: str, action: str | None = None) -> dict | None:
    """-> the stored {action, args, by} once, or None when unknown / expired / wrong action."""
    r = db.one("SELECT value FROM settings WHERE key=?", (f"confirm:{token}",))
    if not r:
        return None
    db.q("DELETE FROM settings WHERE key=?", (f"confirm:{token}",))
    v = _val(r)
    if v["exp"] < db.now_iso() or (action and v["action"] != action):
        return None
    return v
