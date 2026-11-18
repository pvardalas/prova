"""Prova API server. FastAPI on 127.0.0.1:8770. Long work runs as background jobs; progress arrives as events."""
import asyncio
import hmac
import html
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
import logging

from prova import db, flow, template
from prova import guard
from prova.guard import auth, confirm

ROOT = db.ROOT
UI = ROOT / "ui"
UPLOADS = ROOT / "data" / "uploads"
MODULES = ["policy", "gateway", "onboard", "prompts", "sim", "evals", "learn", "sidecar", "voice"]

jobs: dict[str, dict] = {}
_tasks: set = set()
SINGLE = {"ingest": "data", "policy": "policy", "playbook": "playbook", "eval": "safety", "plan": "plan", "simulate": "lab"}


@asynccontextmanager
async def lifespan(app):
    flow.load_env()
    db.init()
    yield


app = FastAPI(title="Prova", lifespan=lifespan)


def _public(path: str) -> bool:
    try:
        from prova import voice
        pub = tuple(voice.PUBLIC_PATHS)
    except Exception:  # noqa: BLE001
        pub = ("/phone", "/api/phone/", "/favicon.ico", "/c/")
    return path.startswith(pub + auth.PUBLIC_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Identity + scope for every REST route (BUILD.md R5). Customer links and the phone pages carry their own token."""
    path = request.url.path
    if _public(path):
        return await call_next(request)
    actor, err = auth.identify(request)
    if err:
        return JSONResponse({"error": err[2], "rule": err[1]}, status_code=err[0])
    err = auth.scope_error(actor, request.method, path)
    if err:
        if actor["role"] == "mcp":
            guard.check(actor, "rest_write", {"method": request.method, "path": path[:300]})
        return JSONResponse({"error": err[2], "rule": err[1]}, status_code=err[0])
    if actor["role"] == "mcp":
        g = guard.check(actor, "rest_read" if request.method in ("GET", "HEAD") else "rest_write",
                        {"method": request.method, "path": path[:300]})
        if g["decision"] != "allow":
            return JSONResponse({"error": g["hint"] or g["rule"], "rule": g["rule"], "decision": g["decision"]}, status_code=429 if g["rule"].startswith("rate") else 403)
    request.state.actor = actor
    return await call_next(request)


@app.exception_handler(flow.ModuleMissing)
async def _missing(request, exc):
    return JSONResponse({"error": f"module {exc.name} not built yet"}, status_code=503)


# ---------------------------------------------------------------- background jobs

def _need(*names):
    for n in names:
        flow._mod(n)


def start_job(kind: str, coro, **meta) -> dict:
    if kind in SINGLE:
        for j in jobs.values():
            if j["kind"] == kind and j["status"] == "running":
                coro.close()
                return {"started": False, "error": f"{kind} already running", "job": j["id"]}
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "kind": kind, "status": "running", "started": db.now_iso(), "finished": None, "error": None,
           "result": None, **meta}
    jobs[jid] = job
    for old in [k for k, v in jobs.items() if v["status"] != "running"][:-40]:
        jobs.pop(old, None)

    async def run():
        try:
            job["result"] = await coro
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
            if kind in SINGLE:
                try:
                    db.set_step(SINGLE[kind], "blocked", job["error"])
                except Exception:  # noqa: BLE001
                    pass
        job["finished"] = db.now_iso()

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return {"started": True, "job": jid}


def _th(fn, *a):
    return asyncio.to_thread(fn, *a)


def _brief(j):
    return {k: v for k, v in j.items() if k != "result"} | {"has_result": j["result"] is not None}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return json.loads(json.dumps(j, default=str))


# ---------------------------------------------------------------- status, reset, clock

def _phone():
    try:
        return flow._mod("voice").phone_status()
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/status")
def status():
    co = db.company() or {}
    modules = {}
    for m in MODULES:
        try:
            flow._mod(m)
            modules[m] = True
        except Exception:  # noqa: BLE001
            modules[m] = False
    missing = []
    try:
        missing = flow._mod("policy").missing_required()
    except Exception:  # noqa: BLE001
        pass
    return {
        "company": {k: co.get(k) for k in ("name", "business_type", "language", "goal_type", "goal_text", "status",
                                           "budget_eur", "policy_version")},
        "setup": co.get("setup") or {},
        "counts": {
            "customers": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM customer GROUP BY status")},
            "rejects": db.one("SELECT COUNT(*) n FROM reject")["n"], "docs": db.one("SELECT COUNT(*) n FROM doc")["n"],
            "cards": db.one("SELECT COUNT(*) n FROM tactic_card")["n"],
            "handoffs_waiting": db.one("SELECT COUNT(*) n FROM handoff WHERE status='waiting'")["n"],
            "followups_pending": db.one("SELECT COUNT(*) n FROM followup WHERE status='pending'")["n"]},
        "missing_required": missing,
        "live_calls": db.q("SELECT id, customer_id, mode, status, purpose FROM call WHERE status IN ('queued','warmup','live') "
                           "AND mode IN ('live','sim')"),
        "cost_total_eur": db.cost_total(), "phone": _phone(), "clock": db.now_iso(),
        "jobs": [_brief(j) for j in jobs.values()], "modules": modules, "fake": os.environ.get("PROVA_FAKE") == "1"}


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True}


@app.get("/api/clock")
def get_clock():
    return {"now": db.now_iso()}


@app.post("/api/clock")
def set_clock(body: dict = Body(default={})):
    if body.get("set"):
        db.set_clock(body["set"])
    if body.get("advance_hours"):
        db.advance_clock(float(body["advance_hours"]))
    return {"now": db.now_iso()}


# ---------------------------------------------------------------- setup

@app.get("/api/samples")
def list_samples():
    out = []
    for s in flow.samples():
        r = flow.resolve_sample(s["name"])
        out.append({**s, "data_path": r["data"], "doc_paths": r["docs"]})
    return out


async def _load_sample(r):
    db.reset()
    res = await _th(flow.ingest_file, r["data"])
    docs = [await _th(flow.add_doc_file, p) for p in r["docs"]]
    return {"ingest": res, "docs": docs, "goal": r["goal"]}


@app.post("/api/setup/sample")
async def setup_sample(body: dict = Body(...)):
    _need("onboard")
    r = flow.resolve_sample(body.get("name", ""))
    if not r:
        raise HTTPException(404, "unknown sample")
    return {**start_job("ingest", _load_sample(r)), "goal": r["goal"]}


def _save_upload(f: UploadFile, data: bytes) -> str:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(f.filename or "upload"))
    p = UPLOADS / name
    p.write_bytes(data)
    return str(p)


@app.post("/api/setup/data")
async def setup_data(file: UploadFile = File(...)):
    _need("onboard")
    path = _save_upload(file, await file.read())
    return start_job("ingest", _th(flow.ingest_file, path))


@app.post("/api/setup/doc")
async def setup_doc(file: UploadFile = File(...)):
    ob = flow._mod("onboard")
    raw = await file.read()
    name = os.path.basename(file.filename or "document")
    _save_upload(file, raw)
    data = raw if name.lower().endswith(".pdf") else raw.decode("utf-8", errors="replace")
    return {"id": await _th(ob.add_document, name, data), "name": name}


@app.post("/api/setup/url")
async def setup_url(body: dict = Body(...)):
    ob = flow._mod("onboard")
    return {"id": await _th(ob.add_url, body["url"])}


@app.post("/api/setup/policy")
async def setup_policy(body: dict = Body(default={})):
    ob = flow._mod("onboard")
    goal = body.get("goal") or (db.company() or {}).get("goal_text") or ""
    return start_job("policy", _th(ob.fill_policy, goal))


@app.post("/api/setup/playbook")
async def setup_playbook():
    return start_job("playbook", _th(flow._mod("onboard").seed_playbooks))


@app.post("/api/setup/eval")
async def setup_eval(body: dict = Body(default={})):
    ev = flow._mod("evals")
    return start_job("eval", ev.run_eval(body.get("only")))


@app.post("/api/setup/approve")
def setup_approve():
    return flow._mod("onboard").approve()


@app.post("/api/setup/live")
def setup_live():
    return flow._mod("onboard").go_live()


@app.post("/api/setup/pause")
def setup_pause(body: dict = Body(default={})):
    return flow._mod("onboard").pause(body.get("reason") or "paused by owner")


# ---------------------------------------------------------------- read endpoints

def _last_outcomes():
    return {r["customer_id"]: r for r in db.q(
        "SELECT customer_id, outcome, id call_id FROM call WHERE id IN (SELECT MAX(id) FROM call "
        "WHERE mode IN ('live','sim') GROUP BY customer_id)")}


@app.get("/api/customers")
def customers():
    last = _last_outcomes()
    out = []
    for c in db.q("SELECT * FROM customer WHERE (flags IS NULL OR flags NOT LIKE '%eval test customer%') ORDER BY id"):  # hide the safety test's synthetic customer
        f = c.get("fields") or {}
        lo = last.get(c["id"]) or {}
        out.append({"id": c["id"], "name": c["name"], "phone": c["phone"], "status": c["status"], "flags": c["flags"],
                    "consent": c["consent"], "score": c["score"], "rank_reason": c["rank_reason"],
                    "attempts": c["attempts"], "last_called_at": c["last_called_at"], "product": f.get("product"),
                    "price": f.get("price"), "expiry_date": f.get("expiry_date"), "last_outcome": lo.get("outcome"),
                    "last_call_id": lo.get("call_id"), "fields": f, "row_no": c["row_no"]})
    return out


@app.get("/api/customers/{cid}")
def customer(cid: int):
    c = db.get("customer", cid)
    if not c:
        raise HTTPException(404, "no such customer")
    h = db.customer_history(cid)
    calls = db.q("SELECT * FROM call WHERE customer_id=? AND mode IN ('live','sim') ORDER BY id", (cid,))
    return {"customer": c, "calls": calls, "followups": h["followups"],
            "handoffs": db.q("SELECT * FROM handoff WHERE customer_id=?", (cid,)),
            "confirmations": db.q("SELECT * FROM confirmation WHERE customer_id=?", (cid,))}


@app.get("/api/customers/{cid}/export")
def customer_export(cid: int):
    return flow.export_customer(cid)


@app.delete("/api/customers/{cid}")
def customer_erase(cid: int, request: Request):
    """The owner (UI) erases directly. An agent only gets a confirmation token that the owner must use."""
    if not db.get("customer", cid):
        raise HTTPException(404, "no such customer")
    actor = getattr(request.state, "actor", {"role": "owner"})
    if actor["role"] != "owner":
        t = confirm.create("erase_customer", {"customer_id": cid}, by=actor.get("name") or "agent")
        return JSONResponse({"status": "owner_confirmation_required", "token": t["token"], "expires": t["expires"],
                             "hint": "Tell the owner. They confirm with POST /api/confirm/{token} on the local UI."}, status_code=202)
    return flow.erase_customer(cid)


@app.get("/api/confirmations")
def confirmations():
    return confirm.pending()


@app.post("/api/confirm/{token}")
def confirm_action(token: str):
    """Owner scope only (enforced by the auth middleware): run the action an agent asked for."""
    v = confirm.consume(token)
    if not v:
        raise HTTPException(404, "unknown, used or expired token")
    if v["action"] == "erase_customer":
        return {"done": v["action"], **flow.erase_customer(int(v["args"]["customer_id"]))}
    raise HTTPException(400, "unknown action")


@app.get("/api/rejects")
def rejects():
    return db.q("SELECT * FROM reject ORDER BY id")


@app.get("/api/ingest")
def ingest_summary():
    return flow.last_ingest()


@app.get("/api/docs")
def docs():
    return [{"id": d["id"], "name": d["name"], "kind": d["kind"], "length": len(d["text"] or "")}
            for d in db.q("SELECT * FROM doc ORDER BY id")]


@app.get("/api/policy")
def policy():
    rows = {r["key"]: r for r in db.q("SELECT * FROM policy_slot")}
    slots = []
    for s in template.POLICY_SLOTS:
        r = rows.get(s["key"]) or {}
        slots.append({"key": s["key"], "label": s["label"], "group": s["group"], "type": s["type"], "required": s["required"],
                      "value": r.get("value"), "source": r.get("source"), "status": r.get("status", "empty")})
    try:
        pol = flow._mod("policy")
        missing, checks = pol.missing_required(), pol.derived_checks()
    except flow.ModuleMissing:
        missing = [{"key": s["key"], "label": s["label"]} for s in slots if s["required"] and s["status"] == "empty"]
        checks = []
    return {"slots": slots, "missing_required": missing, "derived_checks": checks, "floor": template.FLOOR,
            "version": (db.company() or {}).get("policy_version")}


@app.put("/api/policy/{key}")
def policy_confirm(key: str, request: Request, body: dict = Body(default={})):
    if key not in template.SLOT_KEYS:
        raise HTTPException(404, "unknown slot")
    actor = getattr(request.state, "actor", None) or {"role": "owner"}
    by = None if actor["role"] == "owner" else (actor.get("name") or "agent")   # an agent's confirmation is an attestation, listed on the turn-on page
    res = flow._mod("onboard").confirm_slot(key, body.get("value"), attested_by=by)
    try:  # the stepper must follow the policy: unblock the step once nothing required is empty
        missing = flow._mod("policy").missing_required()
        want = "blocked" if missing else "done"
        if (db.company().get("setup") or {}).get("policy") != want:
            db.set_step("policy", want, {"empty": [m["key"] for m in missing]})
    except Exception:  # noqa: BLE001
        pass
    return res


@app.get("/api/playbook")
def playbook(goal: str | None = None):
    try:
        return flow._mod("learn").playbook(goal)
    except flow.ModuleMissing:
        return [{**c, "win_rate": round((c["wins"] + 1) / (c["tries"] + 2), 2)} for c in db.q("SELECT * FROM tactic_card ORDER BY id")]


@app.get("/api/eval")
def eval_latest():
    run = db.one("SELECT * FROM eval_run ORDER BY id DESC LIMIT 1")
    if not run:
        return {"run": None, "results": []}
    return {"run": run, "results": db.q("SELECT * FROM eval_result WHERE run_id=? ORDER BY id", (run["id"],))}


# ---------------------------------------------------------------- plan and calls

@app.post("/api/plan")
async def plan(body: dict = Body(default={})):
    _need("policy")
    goal = body.get("goal") if body.get("goal") in template.GOALS else None
    return start_job("plan", _th(flow.rank_customers, None, goal))


def _why_not(customer: dict, mode: str, via: str) -> str | None:
    """Plain-words reason a call cannot start right now (None = it can). Shown in the UI instead of a silent no-op."""
    try:
        d = flow._mod("policy").may_dial(customer, db.company().get("goal_type") or "renew", mode)
    except Exception as e:  # noqa: BLE001
        return f"dial check failed: {e}"
    if not d.get("allow"):
        hint = {"company_status": " Finish the Safety test step, then press Approve and Go live.",
                "consent": " This customer has no recorded consent for automated calls."}.get(d.get("rule"), "")
        return f"Call refused ({d.get('rule')}): {d.get('reason')}.{hint}"
    if mode == "live" and via in ("auto", "browser"):
        ph = flow._mod("voice").phone_status()
        free = ph.get("browser_phones", 0) - len(ph.get("active_call_ids") or [])
        if free <= 0 and not (via == "auto" and ph.get("twilio")):
            if not ph.get("browser_phones") and ph.get("tunnel") in ("starting", "down"):
                return ("The phone link is being reopened (the free tunnel dropped). Wait a few seconds for the new QR "
                        "code, scan it, tap to turn the demo phone on, and keep that page open.")
            if not ph.get("browser_phones") and not ph.get("phone_url"):
                return ("There is no phone link. Press 'New phone link' (needs cloudflared), or start the server with "
                        "PHONE_TUNNEL=1 or PUBLIC_URL.")
            return ("No phone is connected. Open the phone link (QR) on your mobile, tap to turn the demo phone on, "
                    "and keep that page open." if not ph.get("browser_phones") else "The connected phone is busy with another call.")
    return None


@app.post("/api/demo/load")
def demo_load():
    """Owner: replace the current data with the finished demo run (data/demo.db). Calls are always allowed in demo mode
    (no calling-hours / attempts / gap / budget limits; consent and do-not-call still apply); identity = name only."""
    root = Path(__file__).resolve().parent.parent
    src = Path(os.environ.get("PROVA_DEMO_DB") or next((p for p in (root / "demo" / "demo.db", root / "data" / "demo.db")
                                                         if p.exists()), root / "demo" / "demo.db"))
    if not src.exists():
        raise HTTPException(404, f"demo database not found: {src}")
    if src.resolve() == db.DB_PATH.resolve():
        raise HTTPException(400, "this server already runs on the demo source database; start it with another PROVA_DB")
    db.load_demo(src)
    db.set_slot("identity_check", {"field": "name", "question": "Μιλάω με τον/την ίδιο/α;"}, "owner", status="confirmed")
    try:
        flow.rank_customers(None, None)
    except Exception as e:  # noqa: BLE001  ranking is a nicety here; demo mode still finds a callable customer
        logging.getLogger("prova").warning("demo rank failed: %s", e)
    return {"ok": True, "demo_mode": True, "customers": (db.one("SELECT COUNT(*) n FROM customer") or {}).get("n"),
            "calls": (db.one("SELECT COUNT(*) n FROM call") or {}).get("n")}


@app.get("/api/demo")
def demo_status():
    return {"demo_mode": db.demo_mode()}


@app.get("/api/ready")
def ready(mode: str = "live"):
    """Can a call start right now? -> {ready, problem}. The Calls step shows this before the button is pressed."""
    nxt = flow._next_candidates(1)
    if not nxt:
        return {"ready": False, "problem": "No ranked customer to call. Run the Plan step first."}
    why = _why_not(nxt[0], mode, "auto")
    return {"ready": not why, "problem": why}


@app.post("/api/ready")
def make_ready():
    """One click for a demo: approve + go live (only if the safety test passed), re-rank.
    Returns what it did and what still blocks."""
    did, ob, pol = [], flow._mod("onboard"), flow._mod("policy")
    if not pol.missing_required() and (db.company().get("setup") or {}).get("policy") != "done":
        db.set_step("policy", "done")
    st = db.company()["status"]
    if st == "draft":
        r = ob.approve()
        if r.get("error"):
            return JSONResponse({"error": "Cannot approve: " + r["error"], "did": did}, status_code=409)
        did.append("approved")
        st = "approved"
    if st in ("approved", "paused"):
        r = ob.go_live()
        if r.get("error"):
            return JSONResponse({"error": r["error"], "did": did}, status_code=409)
        did.append("live")
    flow.rank_customers("live")
    return {"did": did, **ready("live")}


@app.post("/api/calls")
async def start_call(body: dict = Body(...)):
    _need("policy", "sim")
    cid, mode = int(body["customer_id"]), body.get("mode", "sim")
    if not db.get("customer", cid):
        raise HTTPException(404, "no such customer")
    if mode == "live":
        _need("voice")
    blocked = _why_not(db.get("customer", cid), mode, body.get("via") or "auto")
    if blocked:
        return JSONResponse({"error": blocked}, status_code=409)
    return start_job("call", flow.run_customer(cid, mode, body.get("via") or "auto"), customer_id=cid, mode=mode)


@app.post("/api/campaign")
async def start_campaign(body: dict = Body(default={})):
    _need("policy", "sim")
    mode = body.get("mode", "sim")
    if mode == "live":
        _need("voice")
    nxt = flow._next_candidates(1)
    if not nxt:
        return JSONResponse({"error": "No customer is ready to call. Rank customers in the Plan step first."}, status_code=409)
    blocked = _why_not(nxt[0], mode, "auto")
    if blocked:
        return JSONResponse({"error": blocked}, status_code=409)
    goal = body.get("goal") if body.get("goal") in template.GOALS else None
    return start_job("campaign", flow.run_campaign(int(body.get("n", 5)), mode, int(body.get("parallel", 3)), goal), mode=mode)


@app.get("/api/calls")
def list_calls(mode: str | None = None, customer_id: int | None = None, parent: int | None = None):
    where, args = [], []
    if parent is not None:
        where.append("c.parent_call_id=?")
        args.append(parent)
    elif mode:
        where.append("c.mode=?")
        args.append(mode)
    else:
        where.append("c.mode IN ('live','sim')")
    if customer_id is not None:
        where.append("c.customer_id=?")
        args.append(customer_id)
    return db.q("SELECT c.*, cu.name customer_name, (SELECT COUNT(*) FROM turn t WHERE t.call_id=c.id) turn_count "
                "FROM call c LEFT JOIN customer cu ON cu.id=c.customer_id WHERE " + " AND ".join(where) + " ORDER BY c.id DESC", args)


@app.get("/api/calls/{call_id}")
def get_call(call_id: int):
    c = db.get("call", call_id)
    if not c:
        raise HTTPException(404, "no such call")
    cu = db.get("customer", c["customer_id"]) if c["customer_id"] else None
    sims = db.q("SELECT * FROM call WHERE parent_call_id=? ORDER BY id", (call_id,))
    for s in sims:
        s["turns"] = db.turns(s["id"])
    return {**c, "customer_name": cu and cu["name"], "turns": db.turns(call_id),
            "events": db.q("SELECT * FROM event WHERE call_id=? ORDER BY id", (call_id,)), "warmups": sims,
            "confirmation": db.one("SELECT * FROM confirmation WHERE call_id=?", (call_id,))}


@app.get("/api/calls/{call_id}/audio")
def call_audio(call_id: int):
    c = db.get("call", call_id)
    for p in (c and c.get("audio_path"), flow.out_dir() / "calls" / f"call_{call_id}.wav"):
        if p and os.path.exists(p):
            return FileResponse(p, media_type="audio/wav")
    raise HTTPException(404, "no audio")


@app.get("/api/followups")
def followups():
    return db.q("SELECT f.*, c.name customer_name FROM followup f LEFT JOIN customer c ON c.id=f.customer_id ORDER BY f.due_at")


@app.post("/api/followups/run")
async def followups_run(body: dict = Body(default={})):
    _need("policy", "sim")
    return start_job("followups", flow.run_due_followups(body.get("mode", "sim")))


@app.get("/api/handoffs")
def handoffs():
    return db.q("SELECT h.*, c.name customer_name FROM handoff h LEFT JOIN customer c ON c.id=h.customer_id ORDER BY h.id DESC")


@app.post("/api/handoffs/{hid}/resolve")
def handoff_resolve(hid: int, body: dict = Body(default={})):
    h = db.get("handoff", hid)
    if not h:
        raise HTTPException(404, "no such handoff")
    ctx = dict(h.get("context") or {})
    if body.get("note"):
        ctx["resolution"] = body["note"]
    db.update("handoff", hid, status="resolved", context=ctx)
    return {"ok": True}


@app.get("/api/report")
def get_report():
    return flow.report()


@app.get("/api/evidence/{call_id}")
def evidence(call_id: int):
    c = db.get("call", call_id)
    if not c:
        raise HTTPException(404, "no such call")
    return {"call": c, "turns": db.turns(call_id),
            "events": db.q("SELECT id, t, type, payload, prev_hash, hash FROM event WHERE call_id=? ORDER BY id", (call_id,)),
            "policy_version": c.get("policy_version"), "policy_version_now": (db.company() or {}).get("policy_version"),
            "policy_slots": db.slots(), "chain": db.verify_chain(), "confirmation": db.one("SELECT * FROM confirmation WHERE call_id=?", (call_id,))}


@app.get("/api/results.csv")
def results_csv():
    return FileResponse(flow.export_results(), media_type="text/csv", filename="results.csv")


# ---------------------------------------------------------------- events

@app.get("/api/events")
def events(after: int = 0, limit: int = 500):
    return db.events_since(after, min(limit, 2000))


@app.get("/api/events/stream")
async def events_stream(request: Request, after: int | None = None):
    last = after if after is not None else int(request.headers.get("last-event-id") or 0)
    if after is None and not request.headers.get("last-event-id"):
        last = (db.one("SELECT COALESCE(MAX(id),0) m FROM event") or {"m": 0})["m"]

    async def gen():
        nonlocal last
        beat = time.time()
        yield "retry: 1500\n\n"
        while not await request.is_disconnected():
            rows = await asyncio.to_thread(db.events_since, last, 200)
            for r in rows:
                last = r["id"]
                yield f"id: {r['id']}\nevent: event\ndata: {json.dumps(r, ensure_ascii=False, default=str)}\n\n"
            if not rows and time.time() - beat > 15:
                beat = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- run_all

@app.post("/api/run_all")
async def run_all(body: dict = Body(...)):
    _need("onboard", "policy", "evals", "sim")
    if body.get("sample"):
        r = flow.resolve_sample(body["sample"])
        if not r:
            raise HTTPException(404, "unknown sample")
        data, docs, goal = r["data"], r["docs"], body.get("goal") or r["goal"]
    else:
        data, docs, goal = body.get("data_path"), body.get("doc_paths") or [], body.get("goal") or ""
    if not data:
        raise HTTPException(400, "give `sample` or `data_path`")
    return start_job("run_all", flow.run_all(data, docs, goal, body.get("urls") or [], int(body.get("n_calls", 3)),
                                              body.get("mode", "sim"), bool(body.get("auto_approve", True))))


# ---------------------------------------------------------------- confirmation page

PAGE = {
    "el": dict(title="Επιβεβαίωση προσφοράς", offer="Η προσφορά σας", price="Τιμή", withdraw="Δικαίωμα υπαναχώρησης",
               button="Επιβεβαιώνω", done="Ευχαριστούμε, η προσφορά επιβεβαιώθηκε.", bad="Ο σύνδεσμος δεν είναι έγκυρος.",
               other="English", other_lang="en"),
    "en": dict(title="Confirm your offer", offer="Your offer", price="Price", withdraw="Right of withdrawal",
               button="I confirm", done="Thank you, your offer is confirmed.", bad="This link is not valid.",
               other="Ελληνικά", other_lang="el"),
}


def _page(body: str, lang: str, token: str = "") -> HTMLResponse:
    p = PAGE[lang]
    sw = f'<a href="/c/{html.escape(token)}?lang={p["other_lang"]}">{p["other"]}</a>' if token else ""
    return HTMLResponse(f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{p['title']}</title><style>body{{font:16px system-ui,sans-serif;max-width:30rem;margin:2rem auto;padding:0 1rem;color:#1d2330}}
.card{{border:1px solid #d9dee8;border-radius:10px;padding:1.2rem}}h1{{font-size:1.25rem;margin-top:0}}
button{{font:inherit;background:#2457d6;color:#fff;border:0;border-radius:8px;padding:.7rem 1.2rem;width:100%;cursor:pointer}}
small{{color:#5b6475}}a{{color:#2457d6}}</style></head><body><div class="card">{body}</div><p style="text-align:right"><small>{sw}</small></p></body></html>""")


@app.get("/c/{token}", response_class=HTMLResponse)
def confirm_page(token: str, lang: str | None = None):
    lang = lang if lang in PAGE else (flow._lang() if flow._lang() in PAGE else "el")
    p = PAGE[lang]
    row = db.one("SELECT * FROM confirmation WHERE token=?", (token,))
    if not row:
        return _page(f"<h1>{p['title']}</h1><p>{p['bad']}</p>", lang)
    o = row["offer"] or {}
    withdraw = (db.slots().get("confirmation") or {}).get("withdrawal_text") or ""
    price = o.get("price")
    if row["status"] == "confirmed":
        return _page(f"<h1>{p['title']}</h1><p>{p['done']}</p>", lang, token)
    return _page(f"""<h1>{p['title']}</h1><p><b>{p['offer']}:</b> {html.escape(str(o.get('name') or o.get('offer_id') or ''))}<br>
<b>{p['price']}:</b> {html.escape(str(price))} EUR{(' / ' + html.escape(str(o['period']))) if o.get('period') else ''}</p>
<p><b>{p['withdraw']}:</b> {html.escape(withdraw)}</p>
<form method="post" action="/c/{html.escape(token)}?lang={lang}"><button type="submit">{p['button']}</button></form>""", lang, token)


@app.post("/c/{token}", response_class=HTMLResponse)
def confirm_post(token: str, lang: str | None = None):
    lang = lang if lang in PAGE else (flow._lang() if flow._lang() in PAGE else "el")
    p = PAGE[lang]
    r = flow.confirm(token)
    return _page(f"<h1>{p['title']}</h1><p>{p['done'] if r.get('ok') else p['bad']}</p>", lang)


# ---------------------------------------------------------------- setup2 (agentic setup), owner, consent, inbox, radar, lanes
# Identity comes from the auth middleware (request.state.actor), never from the request body: an answer cannot name its author.

_OWNER = {"role": "owner", "scope": "owner", "session": "owner-local", "name": "owner"}
_hits: dict = {}


def _actor(request: Request) -> dict:
    return getattr(request.state, "actor", None) or dict(_OWNER)


def _by(actor: dict) -> str:
    return "owner" if actor.get("role") == "owner" else f"agent:{actor.get('name') or 'agent'}"


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _limit(request: Request, key: str, n: int = 20, per: float = 60.0):
    """Tiny in-memory rate limit for the public pages (per client address and route)."""
    ip = request.client.host if request.client else "?"
    now = time.time()
    q = [t for t in _hits.get((key, ip), []) if now - t < per]
    if len(q) >= n:
        raise HTTPException(429, "too many requests, wait a minute")
    q.append(now)
    _hits[(key, ip)] = q


def _sa():
    from prova import setup_agent
    return setup_agent


@app.post("/api/setup2/start")
async def setup2_start(request: Request, body: dict = Body(default={})):
    a = _actor(request)
    company = str(body.get("company") or "").strip()
    if not company:
        raise HTTPException(400, "company is required")
    return await _th(lambda: _sa().start(company, body.get("website") or None, str(body.get("description") or ""),
                                          body.get("face") or "chat", None if a["role"] == "owner" else a.get("name")))


@app.post("/api/setup2/add")
async def setup2_add(request: Request):
    a = _actor(request)
    ct = request.headers.get("content-type") or ""
    if ct.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "send a `file` field or JSON {url}")
        path = _save_upload(f, await f.read())
        return await _th(lambda: _sa().add(path=path, actor=a))
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or not (body.get("url") or body.get("path")):
        raise HTTPException(400, "give a file (multipart `file`) or JSON {url}")
    if body.get("path") and a["role"] == "owner":      # a local path, owner only; an agent uploads the bytes instead
        return await _th(lambda: _sa().add(path=str(body["path"]), actor=a))
    if not body.get("url"):
        raise HTTPException(403, "an agent uploads the file bytes (multipart `file`); local paths are for the owner")
    return await _th(lambda: _sa().add(url=str(body["url"]), actor=a))


@app.post("/api/setup2/answer")
async def setup2_answer(request: Request, body: dict = Body(...)):
    a = _actor(request)
    qid = str(body.get("qid") or body.get("id") or "")
    ans = body.get("answer")
    if qid.startswith("ocr:"):
        from prova import intake
        text = ans.get("text") if isinstance(ans, dict) else ans
        r = await _th(intake.fix_cell, qid[4:], str(text or ""))
        _sa()._session(create=True)
        return {**r, "case_file": _sa().case_file(), "questions": _sa().questions()}
    return await _th(lambda: _sa().answer(qid, ans, by=_by(a), actor=a))


@app.post("/api/setup2/skip")
async def setup2_skip(request: Request, body: dict = Body(...)):
    a = _actor(request)
    return await _th(lambda: _sa().skip(str(body.get("qid") or body.get("id") or ""), by=_by(a), actor=a))


@app.post("/api/setup2/build")
async def setup2_build(request: Request, body: dict = Body(default={})):
    a = _actor(request)
    return start_job("setup_build", _sa().build(actor=a, goals_chosen=body.get("goals") or None))


@app.post("/api/setup2/chat")
async def setup2_chat(request: Request, body: dict = Body(...)):
    a = _actor(request)
    return await _sa().chat(str(body.get("message") or ""), by=_by(a), actor=a)


@app.post("/api/setup2/request_turn_on")
async def setup2_request_turn_on(request: Request):
    a = _actor(request)
    r = await _th(lambda: _sa().request_turn_on(actor=a))
    if a["role"] != "owner" and isinstance(r, dict):   # the link is for the owner's eyes only (owner page), not for an agent
        r = {k: v for k, v in r.items() if k not in ("token", "path")}
        if "error" not in r:
            r["next"] = "The owner sees the request on their owner page (/owner) and taps once."
    return r


@app.post("/api/setup2/resume")
async def setup2_resume():
    return await _th(_sa().resume)          # owner scope only (guard/auth.py _ROUTES)


@app.get("/api/setup2/case")
def setup2_case():
    return _sa().case_file()


@app.get("/api/setup2/questions")
def setup2_questions():
    return _sa().questions()


def _certification() -> dict | None:
    """The numbers of the last certification run, from the eval tables (what evals.run_eval recorded)."""
    run = db.one("SELECT * FROM eval_run ORDER BY id DESC LIMIT 1")
    if not run:
        return None
    rows = db.q("SELECT * FROM eval_result WHERE run_id=?", (run["id"],))

    def tally(cat):
        r = [x for x in rows if x["category"] == cat]
        return {"passed": sum(1 for x in r if x["passed"]), "total": len(r)}
    mut = tally("mutant")
    gb = next((x for x in rows if x["attack_id"] == "guard.benchmark"), None)
    m = re.search(r"false-block rate (\d+)% of (\d+)", (gb or {}).get("evidence") or "")
    cert = {"run_id": run["id"], "status": run["status"], "attacks": tally("attack"), "generated": tally("generated"),
            "setup_attacks": tally("setup_attack"), "checks": tally("slot"), "lanes": tally("lane"),
            "mutants": {"caught": mut["passed"], "total": mut["total"]}, "ok": run["status"] == "done" and not run["failed"],
            "failed": run["failed"]}
    if m:
        blocked = round(int(m[1]) / 100 * int(m[2]))
        cert["guard_false_block_rate"] = f"{blocked}/{m[2]} benign blocked"
    return cert


def _image_url(c: dict) -> dict:
    c = dict(c)
    img = c.pop("image", None)
    if img and c.get("id"):
        c["image_url"] = "/api/intake/image/" + str(c["id"]).split(":", 1)[0]
    return c


@app.get("/api/setup2/review")
def setup2_review():
    from prova import intake
    r = _sa().review()
    cert = _certification()
    s_cert = r.get("certification") or {}
    if cert:
        cert = {**cert, "certified": s_cert.get("certified"), "setup_attacks_line": s_cert.get("setup_attacks")}
    return {**r, "certification": cert or r.get("certification"), "ocr_cells": [_image_url(c) for c in intake.review_cells()]}


@app.get("/api/intake/image/{sha12}")
def intake_image(sha12: str):
    from pathlib import Path

    from prova import intake
    s = next((x for x in intake._load("intake_index", []) if x.startswith(sha12) and re.fullmatch(r"[0-9a-f]{6,64}", sha12)), None)
    rec = intake._load(f"intake:{s}") if s else None
    p = Path((rec or {}).get("image") or "")
    if not rec or not p.is_file() or not p.resolve().is_relative_to(intake.intake_dir().resolve()):
        raise HTTPException(404, "no image")
    return FileResponse(p)


# ---- owner: turn-on page, handoffs, agent requests

def _pending_turn_on() -> dict | None:
    rec = _sa()._load(_sa().TURN_ON_KEY)
    if not rec or rec.get("expires", "") < db.now_iso():
        return None
    S = _sa()._session(create=True)
    atts = [a for a in rec.get("attestations") or [] if str(a.get("by", "")).startswith("agent:")]
    return {"token": rec["token"], "path": f"/t/{rec['token']}", "expires": rec["expires"], "attestations": atts,
            "certification": _certification(), "phase": S.get("phase")}


@app.get("/api/owner")
def owner_data():
    S = _sa()._session(create=True)
    co = db.company() or {}
    return {"company": co.get("name") or S.get("company"), "status": co.get("status"), "pending_turn_on": _pending_turn_on(),
            "handoffs": db.q("SELECT h.*, c.name customer_name FROM handoff h LEFT JOIN customer c ON c.id=h.customer_id WHERE h.status='waiting' ORDER BY h.id DESC"),
            "paused": bool(S.get("paused")), "confirmations": confirm.pending(), "open_questions": len(_sa().questions())}


@app.post("/api/owner/turn_on")
def owner_turn_on(request: Request, body: dict = Body(default={})):
    a = _actor(request)
    r = _sa().turn_on(str(body.get("token") or ""), by=_by(a), actor=a)
    return JSONResponse(r, status_code=400 if "error" in r else 200)


@app.get("/owner", include_in_schema=False)
def owner_page():
    return FileResponse(UI / "owner.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
def login(request: Request, k: str = ""):
    """Owner sign-in over a tunnel: /login?k=PROVA_TOKEN stores the token in a cookie and opens the dashboard."""
    _limit(request, "login", 20)
    owner_t = auth.token()
    if not (k and owner_t and hmac.compare_digest(k, owner_t)):
        return HTMLResponse("<p>Wrong or missing key.</p>", status_code=401)
    r = Response(status_code=303, headers={"Location": "/", "Cache-Control": "no-store"})
    r.set_cookie("prova_token", k, httponly=True, samesite="lax", secure=request.url.scheme == "https"
                 or request.headers.get("x-forwarded-proto") == "https", max_age=7 * 86400)
    return r


@app.get("/t/{token}", include_in_schema=False)
def turn_on_page(request: Request, token: str):
    """The owner's link. HTML for the browser; with Accept: application/json the same token returns what will be turned on."""
    _limit(request, "t", 30)
    if not _wants_json(request):
        return FileResponse(UI / "owner.html", headers={"Cache-Control": "no-store"})
    p = _pending_turn_on()
    if not p or not hmac.compare_digest(str(p["token"]), token):
        return JSONResponse({"error": "this link is not valid (used, expired or wrong)"}, status_code=404)
    co = db.company() or {}
    return {"company": co.get("name"), "status": co.get("status"), "pending_turn_on": {k: v for k, v in p.items() if k != "path"}, "handoffs": []}


@app.post("/t/{token}")
def turn_on_tap(request: Request, token: str):
    """The one human tap. The token is the credential (single use, 60 min); an external agent's identity is refused here."""
    _limit(request, "t", 10)
    actor, err = auth.identify(request)
    if actor and actor["role"] != "owner":
        r = _sa().turn_on(token, by=_by(actor), actor=actor)       # the guard denies and logs it
        return JSONResponse(r if "error" in r else {"error": "only the owner can turn on"}, status_code=403)
    r = _sa().turn_on(token, by="owner", actor=dict(_OWNER))
    return JSONResponse(r, status_code=400 if "error" in r else 200)


# ---- radar, lanes, brief, consent requests

@app.get("/api/radar")
def api_radar(goal: str | None = None, days: int = 45):
    from prova import goals
    return goals.radar(goal, days)


@app.get("/api/lanes")
def api_lanes():
    from prova import compliance
    return compliance.lanes_summary()


@app.get("/api/compliance/brief")
def api_compliance_brief():
    from prova import compliance
    return compliance.brief()


@app.get("/api/brief")
def api_brief():
    from prova import goals
    return goals.brief()


@app.post("/api/consent/send")
async def api_consent_send(body: dict = Body(default={})):
    from prova import compliance
    return await _th(compliance.send_consent_requests, body.get("customer_ids"))


# ---- goals, per-goal playbooks, the lab

@app.get("/api/goals")
def api_goals():
    from prova import goals
    return {"proposals": goals.propose_goals(), "active": goals.active_goals(), "default": (goals.propose_goals() or [{}])[0].get("goal")}


@app.post("/api/goals")
async def api_set_goals(request: Request, body: dict = Body(...)):
    from prova import goals
    sel = body.get("goals") if body.get("goals") is not None else body.get("goal")
    r = await _th(goals.set_goals, sel, _by(_actor(request)))
    return JSONResponse(r, status_code=400) if "error" in r else r


@app.get("/api/playbook/view")
def api_playbook_view(goal: str | None = None):
    from prova import lab
    return lab.playbook_view(goal if goal in template.GOALS else None)


@app.get("/api/playbook/history")
def api_playbook_history(goal: str | None = None):
    from prova import lab
    return lab.history(goal if goal in template.GOALS else None)


@app.get("/api/playbook/cards/{card_id}")
def api_card_detail(card_id: int):
    from prova import lab
    r = lab.card_detail(card_id)
    if "error" in r:
        raise HTTPException(404, r["error"])
    return r


@app.post("/api/lab/simulate")
async def api_lab_simulate(body: dict = Body(default={})):
    from prova import lab
    _need("policy", "sim")
    days = max(1, min(int(body.get("days", 7)), 30))
    cpd = max(1, min(int(body.get("calls_per_day", 4)), 12))
    cap = max(0.0, min(float(body.get("cap_eur", 1.5)), 20.0))
    sel = [g for g in (body.get("goals") or []) if g in template.GOALS] or None
    return start_job("simulate", lab.simulate_period(days, cpd, sel, cap), days=days)


@app.get("/api/lab")
def api_lab():
    from prova import lab
    return lab.overview()


# ---- consent page (existing customer: /k/{token}; QR newcomer: /join), inbox

def _consent_error(r: dict) -> JSONResponse:
    gone = "no longer valid" in str(r.get("error")) or r.get("status") == "expired"
    return JSONResponse({"error": r.get("error"), "status": r.get("status")}, status_code=410 if gone else 404)


async def _call_when_connected(cid: int) -> dict:
    """The consent page opens its socket a moment after the tap (microphone prompt). Wait for it, so the page rings."""
    from prova import voice
    for _ in range(160):
        if voice.bound_phone(cid):
            break
        await asyncio.sleep(0.25)
    else:
        return {"customer_id": cid, "outcome": "no_answer", "note": "the customer's browser did not connect"}
    return await flow.run_customer(cid, mode="live", via="browser")


def _start_browser_call(r: dict):
    if r.get("start_call") and r.get("customer_id") is not None:
        start_job("call", _call_when_connected(int(r["customer_id"])), customer_id=r["customer_id"])


@app.get("/k/{token}", include_in_schema=False)
def consent_get(request: Request, token: str):
    from prova import compliance
    _limit(request, "k", 60)
    if not _wants_json(request):
        return FileResponse(UI / "consent.html", headers={"Cache-Control": "no-store"})
    r = compliance.consent_page(token)
    return _consent_error(r) if "error" in r else r


@app.post("/k/{token}")
async def consent_post(request: Request, token: str, body: dict = Body(default={})):
    from prova import compliance
    _limit(request, "k", 20)
    r = await _th(compliance.consent_answer, token, str(body.get("choice") or ""), body.get("when"))
    if "error" in r:
        return JSONResponse({"error": r["error"]}, status_code=400)
    _start_browser_call(r)
    return {k: v for k, v in r.items() if k != "start_call"}


@app.get("/join", include_in_schema=False)
def join_get(request: Request):
    from prova import compliance
    _limit(request, "join", 60)
    if not _wants_json(request):
        return FileResponse(UI / "consent.html", headers={"Cache-Control": "no-store"})
    lang = flow._lang() if flow._lang() in ("el", "en") else "el"
    return {"company": compliance._company(), "who": compliance._company(), "why": "demo call", "text": compliance.consent_text(lang),
            "text_version": compliance.CONSENT_VERSION, "language": lang, "status": "join"}


@app.post("/join")
async def join_post(request: Request, body: dict = Body(default={})):
    from prova import compliance
    _limit(request, "join", 6)
    r = await _th(compliance.join, body.get("name"), body.get("phone"), body.get("language") or "el", body.get("consent_text_version") or "")
    if "error" in r:
        return JSONResponse({"error": r["error"]}, status_code=400)
    _start_browser_call(r)
    return {k: v for k, v in r.items() if k != "start_call"}


def _inbox(token: str) -> dict | None:
    row = db.one("SELECT * FROM consent_request WHERE token=?", (token,)) if token and len(token) >= 8 else None
    if not row:
        return None
    c = db.get("customer", row["customer_id"]) or {}
    items = []
    for r in db.q("SELECT * FROM consent_request WHERE customer_id=? ORDER BY id DESC", (row["customer_id"],)):
        items.append({"id": f"k{r['id']}", "kind": "consent_request", "status": r["status"], "at": r["sent_at"],
                      "url": f"/k/{r['token']}", "text": r["message"]})
    for r in db.q("SELECT * FROM confirmation WHERE customer_id=? ORDER BY id DESC", (row["customer_id"],)):
        o = r["offer"] or {}
        items.append({"id": f"c{r['id']}", "kind": "confirmation", "status": r["status"], "url": f"/c/{r['token']}",
                      "text": f"{o.get('name') or ''} {o.get('price') or ''}".strip() or None})
    return {"company": (db.slots().get("company_name") if db.company() else None) or (db.company() or {}).get("name"),
            "customer_name": c.get("name"), "items": items}


@app.get("/inbox/{token}", include_in_schema=False)
def inbox_page(request: Request, token: str):
    _limit(request, "inbox", 120)
    if _wants_json(request):
        d = _inbox(token)
        return d if d else JSONResponse({"error": "invalid link"}, status_code=404)
    return FileResponse(UI / "inbox.html", headers={"Cache-Control": "no-store"})


@app.get("/api/inbox/{token}")
def inbox_api(request: Request, token: str):
    _limit(request, "inbox", 120)
    d = _inbox(token)
    return d if d else JSONResponse({"error": "invalid link"}, status_code=404)


@app.get("/api/qr.svg")
def api_qr(request: Request, path: str = "/join"):
    import io

    import segno
    from prova import voice
    if not path.startswith("/") or path.startswith("//") or len(path) > 200:
        raise HTTPException(400, "path must start with /")
    base = (os.environ.get("PUBLIC_URL") or voice.STATE.get("public_url") or str(request.base_url)).rstrip("/")
    buf = io.BytesIO()
    segno.make(base + path, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- UI, voice

@app.get("/", include_in_schema=False)
def index():
    if (UI / "dashboard.html").exists():
        return FileResponse(UI / "dashboard.html")
    return HTMLResponse("<h1>Prova</h1><p>ui/dashboard.html not built yet. The API is at /docs.</p>")


if UI.exists():
    app.mount("/ui", StaticFiles(directory=UI), name="ui")

try:
    from prova import voice

    voice.mount(app)
except Exception as _e:  # noqa: BLE001
    print(f"voice not mounted: {type(_e).__name__}: {_e}")


def main():
    import uvicorn

    flow.load_env()
    host = os.environ.get("PROVA_HOST", "127.0.0.1")
    auth.check_bind(host)
    if os.environ.get("PHONE_TUNNEL") == "1":
        auth.ensure_token()
    uvicorn.run(app, host=host, port=int(os.environ.get("PROVA_PORT", "8770")))


if __name__ == "__main__":
    main()
