"""Rate, budget, loop and denial-streak limits, in memory per process, keyed by actor session."""
import hashlib
import json
import re
import threading
import time
from collections import defaultdict, deque

LIMITS = {"loop_repeat": 3, "denial_streak": 3, "setup_eur": 0.50, "default_rate": "120/min"}
_lock = threading.Lock()
_calls: dict = defaultdict(lambda: defaultdict(deque))     # session -> tool -> timestamps
_total: dict = defaultdict(int)                            # session -> total calls
_last: dict = {}                                           # session -> (key, repeat count)
_streak: dict = defaultdict(int)
_paused: set = set()
_WINDOW = {"min": 60, "hour": 3600, "session": None}


def reset(session: str | None = None):
    with _lock:
        if session is None:
            for d in (_calls, _total, _last, _streak):
                d.clear()
            _paused.clear()
        else:
            _calls.pop(session, None)
            _total.pop(session, None)
            _last.pop(session, None)
            _streak.pop(session, None)
            _paused.discard(session)


def resume(session: str):
    """The owner resumes a paused session."""
    with _lock:
        _paused.discard(session)
        _streak.pop(session, None)


def is_paused(session: str) -> bool:
    return session in _paused


def digest(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


def _parse(rate: str):
    for part in (rate or "").split(","):
        m = re.match(r"\s*(\d+)\s*/\s*(min|hour|session)", part)
        if m:
            yield int(m[1]), m[2]


def check(session: str, tool: dict, args: dict, spent_eur: float = 0.0, role: str = "") -> dict | None:
    """-> None when within limits, else {rule, hint}. Does not count the call (record() does)."""
    if session in _paused:
        return {"rule": "session_paused", "hint": "This session is paused after repeated blocks. Stop and ask the owner to resume."}
    name = tool["name"]
    now = time.time()
    with _lock:
        for n, per in _parse(tool.get("rate") or LIMITS["default_rate"]):
            q = _calls[session][name]
            if per == "session":
                if len(q) >= n:
                    return {"rule": "rate_session", "hint": f"{name} is limited to {n} calls per session."}
            else:
                w = _WINDOW[per]
                recent = sum(1 for t in q if now - t < w)
                if recent >= n:
                    return {"rule": "rate_limit", "hint": f"{name} is limited to {n} per {per}. Slow down."}
        if role == "setup_agent" and tool["effect"] != "read" and spent_eur >= LIMITS["setup_eur"]:
            return {"rule": "budget", "hint": "The setup budget is spent. Stop and tell the owner."}
        if role == "setup_agent" and _total[session] >= 150:
            return {"rule": "session_tool_cap", "hint": "The setup session reached its 150 tool calls."}
        if tool["effect"] != "read":
            key = (name, digest(args))
            last = _last.get(session)
            if last and last[0] == key and last[1] + 1 >= LIMITS["loop_repeat"]:
                return {"rule": "loop", "hint": "You repeated the same action three times. Try something different or ask the owner."}
    return None


def record(session: str, tool: dict, args: dict, decision: str, role: str = ""):
    """Count a finished decision. Three denials in a row pause the session (not for the caller agent)."""
    with _lock:
        _calls[session][tool["name"]].append(time.time())
        _total[session] += 1
        key = (tool["name"], digest(args))
        last = _last.get(session)
        _last[session] = (key, (last[1] + 1) if last and last[0] == key else 1)
        if decision == "allow":
            _streak[session] = 0
        else:
            _streak[session] += 1
            if role != "caller_agent" and _streak[session] >= LIMITS["denial_streak"]:
                _paused.add(session)
