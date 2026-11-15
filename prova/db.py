"""Prova data layer. SQLite, one file, shared by every process (server, calls, MCP).

Single source of truth: everything the UI shows and everything an agent reads over MCP comes from here.
Rows come back as plain dicts; JSON columns are decoded for you.

Time: always use now() from this module, never datetime.now(). The clock can be moved (advance_clock /
set_clock) so calling hours and follow-ups can be tested at any hour.

Events are append-only and hash-chained (each row stores the hash of the previous one), so the record of a
call cannot be edited afterwards without breaking the chain. verify_chain() checks it.
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PROVA_DB", ROOT / "data" / "prova.db"))

JSON_COLS = {"value", "raw", "fields", "consent", "flags", "brief", "cards_used", "outcome_detail", "review",
             "payload", "summary_json", "context", "offer", "units", "lint", "setup", "evidence", "meta"}

OUTCOMES = ["won", "callback", "thinking", "no_answer", "human", "declined", "do_not_call", "failed"]

# Every event type in the system. Emit only these (add here first if you truly need a new one).
EVENT_TYPES = [
    "setup",          # onboarding progress: payload {step, status, detail}
    "policy",         # a slot changed: payload {key, status}
    "card",           # tactic card added / changed: payload {card_id, status, origin}
    "dial_check",     # may_dial result: payload {allow, rule, reason}
    "call_started",   # payload {mode, via, purpose}
    "turn",           # one spoken line: payload {role, text}
    "tool",           # gateway call: payload {name, args, result, allowed, rule}
    "moves",          # live move ranking: payload {situation, candidates:[{card_id, move, score}], chosen, ms}
    "correction",     # agent told to correct itself: payload {said, correct, rule}
    "guard",          # sidecar stopped or redirected a call: payload {action: cut|handoff|stop_selling, reason, rule}
    "warmup",         # pre-call rehearsal: payload {sim_call_id, tactic, result} and finally {brief}
    "call_ended",     # payload {outcome, duration_s, cost_eur}
    "review",         # payload {summary, cards_used, new_card_id}
    "confirmation",   # payload {status: sent|confirmed, channel}
    "followup",       # payload {due_at, reason}
    "handoff",        # payload {reason}
    "eval",           # payload {run_id, attack_id, passed} and finally {run_id, passed, failed}
    "cost",           # payload {kind, eur}
    "consent",        # payload {customer_id, kind: requested|given|refused|withdrawn, channel, text_hash}
    "question",       # payload {id, fact, status: asked|answered|skipped, by}
    "agent_action",   # payload {actor, tool, args_digest, decision: allow|deny|escalate, rule, risk}
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- one company per database (id is always 1)
CREATE TABLE IF NOT EXISTS company (
  id INTEGER PRIMARY KEY, name TEXT, business_type TEXT, language TEXT DEFAULT 'el',
  goal_type TEXT,                         -- key of template.GOALS
  goal_text TEXT,                         -- the owner's goal in their own words
  status TEXT DEFAULT 'draft',            -- draft | approved | live | paused
  budget_eur REAL DEFAULT 5.0,            -- hard spend cap; may_dial denies when cost total reaches it
  policy_version INTEGER DEFAULT 0,       -- bumped every time a slot changes
  setup TEXT DEFAULT '{}',                -- JSON {step: 'done'|'running'|'todo'} for the UI stepper
  created_at TEXT
);

-- uploaded company material (price list, terms, website text)
CREATE TABLE IF NOT EXISTS doc (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, text TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS policy_slot (
  key TEXT PRIMARY KEY,                   -- one of template.SLOT_KEYS
  value TEXT,                             -- JSON
  source TEXT,                            -- where it came from: 'pricelist.pdf: "..."', 'default', 'owner'
  status TEXT DEFAULT 'empty',            -- empty | assumed | filled | confirmed
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS customer (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  row_no INTEGER,                         -- row in the uploaded file
  raw TEXT,                               -- JSON: the row exactly as uploaded
  fields TEXT,                            -- JSON: canonical fields (template.CUSTOMER_FIELDS) + 'extra': {...}
  name TEXT, phone TEXT,                  -- copies of fields.name / fields.phone for quick queries
  consent TEXT,                           -- JSON {automated_calls: true|false|null, source, at}
  status TEXT DEFAULT 'active',           -- active | pending_confirm | won | lost | do_not_call
  flags TEXT DEFAULT '[]',                -- JSON list of data warnings ("phone fixed", "date guessed", ...)
  score REAL, rank_reason TEXT,           -- written by flow.rank_customers
  attempts INTEGER DEFAULT 0, last_called_at TEXT,
  notes TEXT                              -- what the desk has learned, one dated line per call
);

-- rows the ingestor refused, with the reason
CREATE TABLE IF NOT EXISTS reject (
  id INTEGER PRIMARY KEY AUTOINCREMENT, row_no INTEGER, raw TEXT, reason TEXT
);

CREATE TABLE IF NOT EXISTS tactic_card (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  situation TEXT,                         -- short key: price_objection | no_time | already_switched | wants_to_think | distrust_ai | opening | other
  title TEXT, move TEXT,                  -- move: what to do, one or two sentences
  example TEXT,                           -- one example line in the call language
  origin TEXT,                            -- online | learned | seed
  source TEXT,                            -- URL / "call 12"
  tries INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
  status TEXT DEFAULT 'candidate',        -- candidate | proven | retired | rejected
  lint TEXT,                              -- JSON {ok, reasons[]}
  created_at TEXT,
  goal TEXT                               -- key of template.GOALS this card was written for (NULL = any goal)
);

-- one row per card per simulated day (lab.simulate_period): the playbook as it stood at the end of that day
CREATE TABLE IF NOT EXISTS playbook_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, goal TEXT, card_id INTEGER, status TEXT, tries INTEGER, wins INTEGER
);

CREATE TABLE IF NOT EXISTS call (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER,
  purpose TEXT,                           -- goal type of the call
  mode TEXT DEFAULT 'sim',                -- warmup | sim | eval | live
  via TEXT,                               -- text | browser | twilio
  parent_call_id INTEGER,                 -- warm-up sims point at the real call they prepare
  eval_run_id INTEGER, attack_id TEXT,
  status TEXT DEFAULT 'queued',           -- queued | warmup | live | ended | failed
  policy_version INTEGER,
  brief TEXT,                             -- JSON written by sim.warmup
  explore INTEGER DEFAULT 0,              -- 1 = this call tries an experimental tactic
  cards_used TEXT DEFAULT '[]',           -- JSON list of tactic_card ids
  outcome TEXT, outcome_detail TEXT,      -- outcome: one of OUTCOMES; detail JSON {offer_id, price, discount_pct, callback_at, reason, note}
  summary TEXT, review TEXT, audio_path TEXT,
  cost_eur REAL DEFAULT 0,
  started_at TEXT, ended_at TEXT, duration_s REAL
);

CREATE TABLE IF NOT EXISTS turn (
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER,
  role TEXT,                              -- agent | customer | tool | system
  text TEXT, t TEXT, meta TEXT
);

CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT, type TEXT, call_id INTEGER, customer_id INTEGER,
  payload TEXT, prev_hash TEXT, hash TEXT
);

CREATE TABLE IF NOT EXISTS eval_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
  status TEXT DEFAULT 'running',          -- running | done | failed
  policy_version INTEGER, passed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eval_result (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, attack_id TEXT, name TEXT,
  category TEXT,                          -- slot | attack
  passed INTEGER, detail TEXT, evidence TEXT, call_id INTEGER
);

CREATE TABLE IF NOT EXISTS followup (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, source_call_id INTEGER,
  due_at TEXT, reason TEXT,               -- callback | thinking | no_answer
  status TEXT DEFAULT 'pending'           -- pending | done | cancelled
);

CREATE TABLE IF NOT EXISTS handoff (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, call_id INTEGER, reason TEXT,
  context TEXT,                           -- JSON card for the human: who, what was said, what they want
  status TEXT DEFAULT 'waiting', created_at TEXT
);

CREATE TABLE IF NOT EXISTS confirmation (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE, customer_id INTEGER, call_id INTEGER,
  offer TEXT,                             -- JSON {offer_id, name, price, discount_pct, period}
  channel TEXT, message TEXT,
  status TEXT DEFAULT 'sent',             -- sent | confirmed | expired
  sent_at TEXT, confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS consent_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE, customer_id INTEGER,
  channel TEXT,                           -- sms | email | qr | web
  message TEXT, text_hash TEXT,
  status TEXT DEFAULT 'sent',             -- sent | call_now | scheduled | stopped | expired
  sent_at TEXT, answered_at TEXT, scheduled_for TEXT
);

CREATE TABLE IF NOT EXISTS cost (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT, call_id INTEGER,
  kind TEXT,                              -- llm | voice | sms
  model TEXT, units TEXT, eur REAL
);
"""

_lock = threading.Lock()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in d:
        if k in JSON_COLS and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _enc(fields):
    return {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLS and not isinstance(v, str) or isinstance(v, (dict, list)) else v)
            for k, v in fields.items()}


def q(sql, args=()):
    """SELECT -> list of dicts. Also runs writes (returns [])."""
    with connect() as c:
        cur = c.execute(sql, args)
        return [_row(r) for r in cur.fetchall()] if cur.description else []


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def insert(table, **fields):
    f = _enc(fields)
    with connect() as c:
        cur = c.execute(f"INSERT INTO {table} ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
        return cur.lastrowid


def update(table, row_id, **fields):
    """Update by id (policy_slot: by key)."""
    f = _enc(fields)
    pk = "key" if table == "policy_slot" else "id"
    with connect() as c:
        c.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in f)} WHERE {pk}=?", [*f.values(), row_id])


def get(table, row_id):
    pk = "key" if table == "policy_slot" else "id"
    return one(f"SELECT * FROM {table} WHERE {pk}=?", (row_id,))


# ---------------------------------------------------------------- clock

def _offset_h() -> float:
    r = one("SELECT value FROM settings WHERE key='clock_offset_h'")
    return float(r["value"]) if r else 0.0


def now() -> datetime:
    return datetime.now() + timedelta(hours=_offset_h())


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def advance_clock(hours: float) -> str:
    q("INSERT INTO settings (key, value) VALUES ('clock_offset_h', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (str(_offset_h() + float(hours)),))
    return now_iso()


def set_clock(iso: str) -> str:
    """Move the desk clock to an exact local time, e.g. '2026-11-16T10:30'."""
    target = datetime.fromisoformat(iso)
    q("INSERT INTO settings (key, value) VALUES ('clock_offset_h', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (str((target - datetime.now()).total_seconds() / 3600),))
    return now_iso()


# ---------------------------------------------------------------- events (hash chain)

def emit(type, call_id=None, customer_id=None, payload=None):
    """Append one event. Returns its id. Types must be in EVENT_TYPES."""
    assert type in EVENT_TYPES, f"unknown event type {type}"
    body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    with _lock, connect() as c:
        c.execute("BEGIN IMMEDIATE")
        last = c.execute("SELECT hash FROM event ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else ""
        t = now_iso()
        h = hashlib.sha256(f"{prev}|{t}|{type}|{call_id}|{customer_id}|{body}".encode()).hexdigest()
        cur = c.execute("INSERT INTO event (t, type, call_id, customer_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                        (t, type, call_id, customer_id, body, prev, h))
        return cur.lastrowid


def events_since(last_id=0, limit=500):
    return q("SELECT * FROM event WHERE id>? ORDER BY id LIMIT ?", (last_id, limit))


def verify_chain() -> dict:
    """Recompute every hash. -> {ok, count, broken_at: event id or None}."""
    prev, n = "", 0
    with connect() as c:
        for r in c.execute("SELECT * FROM event ORDER BY id"):
            h = hashlib.sha256(f"{prev}|{r['t']}|{r['type']}|{r['call_id']}|{r['customer_id']}|{r['payload']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != h:
                return {"ok": False, "count": n, "broken_at": r["id"]}
            prev, n = r["hash"], n + 1
    return {"ok": True, "count": n, "broken_at": None}


def add_turn(call_id, role, text, meta=None):
    """Store one line of a call and emit a `turn` event. role: agent | customer | tool | system."""
    text = (text or "").strip()
    if not text:
        return None
    call = get("call", call_id)
    tid = insert("turn", call_id=call_id, role=role, text=text, t=now_iso(), meta=meta or {})
    emit("turn", call_id=call_id, customer_id=call["customer_id"] if call else None, payload={"role": role, "text": text})
    return tid


def turns(call_id):
    return q("SELECT * FROM turn WHERE call_id=? ORDER BY id", (call_id,))


# ---------------------------------------------------------------- company, policy, cost

def company() -> dict:
    return get("company", 1)


def slots() -> dict:
    """Current policy as {key: value}. Empty slots are missing from the dict."""
    from prova.template import SLOT_KEYS
    return {r["key"]: r["value"] for r in q("SELECT * FROM policy_slot WHERE status<>'empty'") if r["key"] in SLOT_KEYS}


def set_slot(key, value, source, status="filled"):
    """Write one policy slot, bump the policy version, emit a `policy` event."""
    from prova.template import SLOT_KEYS
    assert key in SLOT_KEYS, f"unknown slot {key}"
    v = json.dumps(value, ensure_ascii=False)
    q("INSERT INTO policy_slot (key, value, source, status, updated_at) VALUES (?,?,?,?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, status=excluded.status, updated_at=excluded.updated_at",
      (key, v, source, status, now_iso()))
    q("UPDATE company SET policy_version = policy_version + 1 WHERE id=1")
    emit("policy", payload={"key": key, "status": status})


def add_cost(kind, eur, call_id=None, model=None, units=None):
    """Record spend. Adds to the call's cost_eur when call_id is given."""
    eur = round(float(eur), 6)
    insert("cost", t=now_iso(), call_id=call_id, kind=kind, model=model, units=units or {}, eur=eur)
    if call_id:
        q("UPDATE call SET cost_eur = COALESCE(cost_eur, 0) + ? WHERE id=?", (eur, call_id))


def cost_total() -> float:
    return round((one("SELECT COALESCE(SUM(eur), 0) s FROM cost") or {"s": 0})["s"], 4)


def set_step(step, status, detail=None):
    """Onboarding stepper state for the UI. status: todo | running | done | blocked."""
    c = company()
    setup = dict(c.get("setup") or {})
    setup[step] = status
    update("company", 1, setup=setup)
    emit("setup", payload={"step": step, "status": status, "detail": detail})


# ---------------------------------------------------------------- views

def customer_view(c: dict, verified: bool = False) -> dict:
    """What the caller agent may see about a customer BEFORE identity is verified: name and the reason
    for the call only. After verification: all canonical fields. Raw rows are never given to the agent."""
    f = dict(c.get("fields") or {})
    if verified:
        f.pop("extra", None)
        return {"id": c["id"], **f, "notes": c.get("notes")}
    keep = ("name", "product", "expiry_date", "due_date", "last_active_date")
    return {"id": c["id"], **{k: f[k] for k in keep if f.get(k) is not None}}


def customer_history(customer_id):
    return {"customer": get("customer", customer_id),
            "calls": q("SELECT * FROM call WHERE customer_id=? AND mode='live' ORDER BY id", (customer_id,)),
            "followups": q("SELECT * FROM followup WHERE customer_id=? ORDER BY id", (customer_id,))}


def due_followups():
    return q("SELECT * FROM followup WHERE status='pending' AND due_at<=? ORDER BY due_at", (now_iso(),))


# ---------------------------------------------------------------- init

def demo_mode() -> bool:
    """Demo databases (Load demo) relax scheduling limits so a call can always be started."""
    try:
        r = one("SELECT value FROM settings WHERE key='demo_mode'")
        return bool(r) and str(r["value"]) == "1"
    except Exception:  # noqa: BLE001
        return False


def load_demo(src) -> None:
    """Replace this database with a finished demo run (sqlite backup API, safe while the server runs)."""
    import sqlite3 as _sq
    s = _sq.connect(f"file:{src}?mode=ro", uri=True)
    d = connect()
    try:
        s.backup(d)
    finally:
        s.close()
    q("INSERT INTO settings (key, value) VALUES ('demo_mode', '1') ON CONFLICT(key) DO UPDATE SET value='1'")
    q("INSERT INTO settings (key, value) VALUES ('clock_offset_h', '0') ON CONFLICT(key) DO UPDATE SET value='0'")
    d.close()


def reset():
    """Wipe everything and start a fresh, empty company in 'draft'."""
    with connect() as c:
        for t in ("settings", "company", "doc", "policy_slot", "customer", "reject", "tactic_card", "call", "turn",
                  "event", "eval_run", "eval_result", "followup", "handoff", "confirmation", "consent_request", "cost",
                  "playbook_snapshot"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    init()


def init():
    from prova.template import POLICY_SLOTS
    with connect() as c:
        c.executescript(SCHEMA)
        if "goal" not in {r["name"] for r in c.execute("PRAGMA table_info(tactic_card)")}:
            c.execute("ALTER TABLE tactic_card ADD COLUMN goal TEXT")      # databases made before per-goal playbooks
    if not company():
        insert("company", id=1, status="draft", created_at=datetime.now().isoformat(timespec="seconds"), setup={})
    have = {r["key"] for r in q("SELECT key FROM policy_slot")}
    for s in POLICY_SLOTS:
        if s["key"] in have:
            continue
        if s["default"] is not None and s["default"] != []:
            insert("policy_slot", key=s["key"], value=json.dumps(s["default"], ensure_ascii=False), source="default",
                   status="assumed", updated_at=now_iso())
        else:
            insert("policy_slot", key=s["key"], value=json.dumps(s["default"]), source=None, status="empty", updated_at=now_iso())


if __name__ == "__main__":
    reset()
    print("fresh database at", DB_PATH, verify_chain())
