"""The lab: learning over time in simulation, and the certification detail for presenting.

simulate_period()  N simulated days of sim-mode calls per active goal; every call is reviewed (new candidate cards, tries and
                   wins updated), the promotion / retirement rules run once per day, and the playbook is snapshotted per day.
                   Nothing outside the calls changes: customers keep their status, attempts and last_called_at (sim mode),
                   and the desk clock is put back at the end. Spend is capped: the run stops when the cost since the start
                   reaches cap_eur.
overview()         the last certification run in detail, and the rehearsals + brief of recent calls.
"""
import asyncio
import re
from datetime import timedelta

from prova import db, flow, learn, llm, template
from prova import goals as goals_mod

DAY_HOUR = "10:30"
_LAB_STEP = "lab_day"


def _explore_card(goal: str, used: set) -> dict | None:
    rows = [r for r in db.q("SELECT * FROM tactic_card WHERE status='candidate'") if r.get("goal") in (goal, None, "") and r["id"] not in used]
    rows.sort(key=lambda r: (r.get("goal") != goal, r["tries"], r["id"]))
    return rows[0] if rows else None


def _snapshot(day: str, goal: str) -> int:
    db.q("DELETE FROM playbook_snapshot WHERE day=? AND goal=?", (day, goal))
    n = 0
    for c in db.q("SELECT * FROM tactic_card ORDER BY id"):
        if c.get("goal") in (goal, None, ""):
            db.insert("playbook_snapshot", day=day, goal=goal, card_id=c["id"], status=c["status"], tries=c["tries"], wins=c["wins"])
            n += 1
    return n


def _spent(start: float) -> float:
    return round(db.cost_total() - start, 4)


async def _one_call(customer_id: int, goal: str, force: dict | None) -> dict:
    """One sim-mode call, the same path as flow.run_customer (warm-up rehearsals, text call, close) but reviewed without
    the automatic status change: the day's rules decide that."""
    sim = flow._mod("sim")
    policy = flow._mod("policy")
    c = db.get("customer", customer_id)
    d = policy.may_dial(c, goal, "sim")
    if not d.get("allow"):
        return {"customer_id": customer_id, "skipped": d.get("rule")}
    call_id = flow.make_call(customer_id, "sim", via="text", purpose=goal)
    try:
        db.update("call", call_id, status="warmup")
        await sim.warmup(call_id)
        db.update("call", call_id, status="live")
        if force:
            db.update("call", call_id, cards_used=[force["id"]], explore=1)
            res = await sim.run_text_call(call_id, force_card_id=force["id"])
        else:
            res = await sim.run_text_call(call_id)
        flow.close_call(call_id)
        rev = learn.review_call(call_id, auto_status=False)
    except Exception as e:  # noqa: BLE001
        flow._fail_call(call_id, f"{type(e).__name__}: {e}")
        return {"customer_id": customer_id, "call_id": call_id, "outcome": "failed", "error": str(e)[:200]}
    row = db.get("call", call_id)
    return {"customer_id": customer_id, "call_id": call_id, "outcome": row.get("outcome"), "turns": res.get("turns"),
            "new_card_id": (rev or {}).get("new_card_id"), "explore_card_id": force["id"] if force else None}


async def simulate_period(days: int = 7, calls_per_day: int = 4, goals: list[str] | None = None, cap_eur: float = 1.5) -> dict:
    """-> {days: [{day, goal, calls, won, win_rate, new_cards, rejected_cards, promoted, retired, explored}], cost_eur, stopped_by, goals}.
    stopped_by: 'days_done' | 'cap' | 'no_candidates'."""
    db.init()
    chosen = [g for g in (goals or goals_mod.active_goals()) if g in template.GOALS]
    if not chosen:
        return {"error": "no active goal: choose goals first (set_goals)", "days": [], "cost_eur": 0.0, "stopped_by": "no_goal"}
    days, calls_per_day = max(1, int(days)), max(1, int(calls_per_day))
    start_cost = db.cost_total()
    offset = db.one("SELECT value FROM settings WHERE key='clock_offset_h'")
    base = db.now().date()
    timeline, stopped = [], "days_done"

    ranked = {}
    for g in chosen:
        ranked[g] = [r for r in await asyncio.to_thread(flow.rank_customers, "sim", g, False) if r.get("callable")]
        ranked[g].sort(key=lambda r: (-(r["score"] or 0), r["customer_id"]))
    if not any(ranked.values()):
        return {"goals": chosen, "days": [], "cost_eur": 0.0, "stopped_by": "no_candidates"}

    try:
        for d in range(days):
            day = (base + timedelta(days=d)).isoformat()
            db.set_clock(f"{day}T{DAY_HOUR}")
            db.emit("setup", payload={"step": _LAB_STEP, "status": "running", "detail": {"day": d + 1, "of": days, "date": day}})
            for g in chosen:
                pool = ranked[g]
                row = {"day": day, "goal": g, "calls": 0, "won": 0, "win_rate": None, "new_cards": 0, "rejected_cards": 0,
                       "promoted": 0, "retired": 0, "explored": None}
                if pool and _spent(start_cost) < cap_eur:
                    used: set = set()
                    for i in range(calls_per_day):
                        if _spent(start_cost) >= cap_eur:
                            stopped = "cap"
                            break
                        cust = pool[(d * calls_per_day + i) % len(pool)]
                        force = _explore_card(g, used) if i == 0 else None
                        if force:
                            used.add(force["id"])
                            row["explored"] = force["id"]
                        r = await _one_call(cust["customer_id"], g, force)
                        if r.get("skipped"):
                            continue
                        row["calls"] += 1
                        row["won"] += int(r.get("outcome") == "won")
                        if r.get("new_card_id"):
                            nc = db.get("tactic_card", r["new_card_id"])
                            row["new_cards" if nc["status"] == "candidate" else "rejected_cards"] += 1
                        db.emit("setup", payload={"step": _LAB_STEP, "status": "running",
                                                  "detail": {"day": d + 1, "of": days, "goal": g, "calls": row["calls"], "won": row["won"]}})
                changes = learn.apply_rules(g)
                row["promoted"] = sum(1 for c in changes if c["to"] == "proven")
                row["retired"] = sum(1 for c in changes if c["to"] == "retired")
                row["win_rate"] = round(row["won"] / row["calls"], 3) if row["calls"] else None
                _snapshot(day, g)
                timeline.append(row)
            if stopped == "cap":
                break
    finally:
        if offset:
            db.q("INSERT INTO settings (key, value) VALUES ('clock_offset_h', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (offset["value"],))
        else:
            db.q("DELETE FROM settings WHERE key='clock_offset_h'")
    db.emit("setup", payload={"step": _LAB_STEP, "status": "done" if stopped == "days_done" else "stopped",
                              "detail": {"days": len(timeline), "stopped_by": stopped, "cost_eur": _spent(start_cost)}})
    return {"goals": chosen, "days": timeline, "cost_eur": _spent(start_cost), "stopped_by": stopped, "cap_eur": cap_eur,
            "fake": llm.FAKE}


# ------------------------------------------------------------------ playbook views

def playbook_view(goal: str | None = None) -> dict:
    """Cards of a goal (and cards for any goal) by status, with origin and the lint reason of rejected ones."""
    cards = learn.playbook(goal)
    used: dict = {}
    for r in db.q("SELECT cards_used FROM call WHERE mode IN ('live','sim')"):
        for i in r.get("cards_used") or []:
            used[i] = used.get(i, 0) + 1
    for c in cards:
        c["origin_text"] = ({"online": "seed (model knowledge or web)", "seed": "seed", "learned": f"learned from {c.get('source')}"}
                            .get(c["origin"], c["origin"]))
        c["call_id"] = int(m[1]) if (m := re.match(r"call (\d+)", str(c.get("source") or ""))) else None
        c["used_in_calls"] = used.get(c["id"], 0)
    by = {s: [c for c in cards if c["status"] == s] for s in ("candidate", "proven", "retired", "rejected")}
    return {"goal": goal, "goals": goals_mod.active_goals(), "counts": {k: len(v) for k, v in by.items()}, "cards": by}


def history(goal: str | None = None) -> dict:
    """Win rate per day (calls of this goal), the playbook counts per day from the snapshots, and the change log of the cards."""
    where, args = ("WHERE goal=?", (goal,)) if goal else ("", ())
    snaps = db.q(f"SELECT * FROM playbook_snapshot {where} ORDER BY day, card_id", args)
    days: dict = {}
    for s in snaps:
        d = days.setdefault(s["day"], {"day": s["day"], "cards": {"candidate": 0, "proven": 0, "retired": 0, "rejected": 0}, "tries": 0, "wins": 0})
        d["cards"][s["status"]] = d["cards"].get(s["status"], 0) + 1
        if s["status"] in ("candidate", "proven"):
            d["tries"] += s["tries"]
            d["wins"] += s["wins"]
    for d in days.values():
        d["card_win_rate"] = round(d["wins"] / d["tries"], 3) if d["tries"] else None
        cs = db.q("SELECT outcome FROM call WHERE mode IN ('live','sim') AND substr(started_at,1,10)=?" + (" AND purpose=?" if goal else ""),
                  (d["day"], goal) if goal else (d["day"],))
        d["calls"], d["won"] = len(cs), sum(1 for c in cs if c["outcome"] == "won")
        d["win_rate"] = round(d["won"] / d["calls"], 3) if d["calls"] else None
    return {"goal": goal, "days": [days[k] for k in sorted(days)], "changes": change_log(goal)}


def change_log(goal: str | None = None) -> list[dict]:
    """born / promoted / retired / rejected, from the `card` events: a row only when a card first appears or its status changes."""
    last: dict = {}
    out = []
    for e in db.q("SELECT * FROM event WHERE type='card' ORDER BY id"):
        p = e["payload"] or {}
        cid, st = p.get("card_id"), p.get("status")
        card = db.get("tactic_card", cid) if cid else None
        if not card or (goal and card.get("goal") not in (goal, None, "")):
            continue
        prev = last.get(cid)
        last[cid] = st
        if prev == st:
            continue
        kind = "born" if prev is None else {"proven": "promoted", "retired": "retired", "rejected": "rejected"}.get(st, st)
        if prev is None and st == "rejected":
            kind = "rejected"
        reason = p.get("reason")
        if not reason and st == "rejected":
            reason = "; ".join((card.get("lint") or {}).get("reasons") or [])
        out.append({"t": e["t"], "card_id": cid, "title": card["title"], "kind": kind, "status": st, "from": prev, "origin": p.get("origin"),
                    "reason": reason or ("seed card" if prev is None and p.get("origin") != "learned" else ""), "call_id": e["call_id"],
                    "goal": card.get("goal")})
    return out


def card_detail(card_id: int) -> dict:
    """A card, the call that taught it (transcript) and the calls that used it."""
    c = db.get("tactic_card", card_id)
    if not c:
        return {"error": "no such card"}
    taught = None
    m = re.match(r"call (\d+)", str(c.get("source") or ""))
    if m and db.get("call", int(m[1])):
        cid = int(m[1])
        call = db.get("call", cid)
        taught = {"call_id": cid, "outcome": call.get("outcome"), "summary": call.get("summary"),
                  "turns": [{"role": t["role"], "text": t["text"]} for t in db.turns(cid) if t["role"] in ("agent", "customer")]}
    used = []
    for r in db.q("SELECT id, customer_id, purpose, mode, outcome, started_at, cards_used FROM call WHERE mode IN ('live','sim') ORDER BY id DESC LIMIT 400"):
        if card_id in (r.get("cards_used") or []):
            used.append({k: r[k] for k in ("id", "purpose", "mode", "outcome", "started_at")})
    return {"card": c, "taught_by": taught, "used_in": used[:30]}


# ------------------------------------------------------------------ certification detail

def _rule_of(evidence: str) -> str | None:
    m = re.search(r"->\s*(\w+)\s*\(([^)]*)\)", evidence or "")
    return f"{m[2]} ({m[1]})" if m else None


def _transcript(call_id):
    if not call_id:
        return None
    return [{"role": t["role"], "text": t["text"]} for t in db.turns(call_id) if t["role"] in ("agent", "customer", "tool")]


def overview(call_limit: int = 12) -> dict:
    """The last eval run in detail + the rehearsals and brief of recent live / sim calls."""
    db.init()
    run = db.one("SELECT * FROM eval_run ORDER BY id DESC LIMIT 1")
    backend = "FAKE mode: scripted agent and twin, no model" if llm.FAKE else "real model"
    out = {"fake": llm.FAKE, "backend": backend,
           "labels": {"attacks": f"Played against the agent by the rules backend ({backend}).",
                      "mutants": "Deliberate weakenings of the policy, caught by the code checks (no model).",
                      "guard": "Labelled actions through Agent Guard rules (code)."}}
    if not run:
        return {**out, "run": None, "calls": _calls(call_limit)}
    rows = db.q("SELECT * FROM eval_result WHERE run_id=? ORDER BY id", (run["id"],))

    def cat(*names):
        return [r for r in rows if r["category"] in names]

    def attack(r):
        return {"id": r["attack_id"], "name": r["name"], "passed": bool(r["passed"]), "script": r["detail"], "evidence": r["evidence"],
                "call_id": r["call_id"], "transcript": _transcript(r["call_id"])}
    try:
        from prova import sandbox
        patches = {n: (sandbox._brief(p) if p else None) for n, p, _ in sandbox.mutant_list()}
    except Exception:  # noqa: BLE001
        patches = {}
    mutants = []
    for r in cat("mutant"):
        name = r["attack_id"].split(".", 1)[-1]
        mutants.append({"name": name, "weakened": patches.get(name), "caught": bool(r["passed"]), "caught_by": r["evidence"].replace("failed checks: ", "")
                        if r["passed"] else None, "evidence": r["evidence"]})
    gb = next((r for r in rows if r["attack_id"] == "guard.benchmark"), None)
    bench = None
    if gb:
        m = re.search(r"catch rate (\d+)% of (\d+); false-block rate (\d+)% of (\d+)", gb["evidence"] or "")
        bench = {"catch_rate_pct": int(m[1]), "malicious": int(m[2]), "false_block_rate_pct": int(m[3]), "benign": int(m[4]),
                 "passed": bool(gb["passed"]), "evidence": gb["evidence"]} if m else {"passed": bool(gb["passed"]), "evidence": gb["evidence"]}
    out.update({"run": run, "fixed_attacks": [attack(r) for r in cat("attack")], "generated_attacks": [attack(r) for r in cat("generated")],
                "setup_attacks": [{**attack(r), "denied_by": _rule_of(r["evidence"])} for r in cat("setup_attack")],
                "mutants": mutants, "guard_benchmark": bench,
                "checks": [{"id": r["attack_id"], "name": r["name"], "passed": bool(r["passed"]), "evidence": r["evidence"]} for r in cat("slot", "lane")],
                "calls": _calls(call_limit)})
    return out


def _calls(limit: int) -> list[dict]:
    res = []
    for c in db.q("SELECT * FROM call WHERE mode IN ('live','sim') ORDER BY id DESC LIMIT ?", (limit,)):
        sims = []
        for s in db.q("SELECT * FROM call WHERE parent_call_id=? ORDER BY id", (c["id"],)):
            note = ""
            ev = db.one("SELECT payload FROM event WHERE type='warmup' AND payload LIKE ? ORDER BY id LIMIT 1", (f'%"sim_call_id": {s["id"]}%',))
            if ev:
                note = ((ev["payload"] or {}).get("result") or {}).get("note") or ""
            persona = note.split(":", 1)[0] if ":" in note else None
            sims.append({"sim_call_id": s["id"], "twin_persona": persona, "outcome": s["outcome"], "turns": len([t for t in db.turns(s["id"]) if t["role"] in ("agent", "customer")]),
                         "cards_used": s.get("cards_used")})
        cu = db.get("customer", c["customer_id"]) or {}
        res.append({"call_id": c["id"], "customer": cu.get("name"), "mode": c["mode"], "purpose": c["purpose"], "outcome": c["outcome"],
                    "started_at": c["started_at"], "brief": c.get("brief"), "rehearsals": sims,
                    "fake": llm.FAKE})
    return res
