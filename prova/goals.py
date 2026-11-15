"""Goals and radar: what is worth calling about, how much money is at stake, and which lane each customer is in.

    suggest_goal()      goal proposal from the columns the customer file has (expiry_date -> renew, ...)
    propose_goals()     every goal the data supports, with counts and money; set_goals() activates 1..n of them
    radar(goal, days)   the customers due for that goal, € at stake, the compliance lane per customer
    brief()             one screen for the owner: goal, radar, lanes, open questions
"""
import json
from datetime import date

from prova import compliance, db, template

DEFAULT_DAYS = 45
LAPSED_DAYS = 90
GOAL_ORDER = ["renew", "collect", "remind", "win_back"]
_TEXT = {
    "renew": {"el": "Θέλω να ανανεώσω τα συμβόλαια που λήγουν τις επόμενες {days} ημέρες.",
              "en": "Renew the contracts that expire in the next {days} days."},
    "collect": {"el": "Θέλω να εισπράξω τις οφειλές που είναι ληξιπρόθεσμες.", "en": "Collect the overdue amounts."},
    "remind": {"el": "Θέλω να υπενθυμίσω στους πελάτες ό,τι λήγει ή προγραμματίζεται τις επόμενες {days} ημέρες.",
               "en": "Remind customers of what is due in the next {days} days."},
    "win_back": {"el": "Θέλω να φέρω πίσω πελάτες που έχουν να έρθουν πάνω από {lapsed} ημέρες.",
                 "en": "Win back customers who have not been active for more than {lapsed} days."},
}
_DATE_KEY = {"renew": "expiry_date", "win_back": "last_active_date", "remind": "due_date", "collect": "due_date"}


def _date(v):
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _customers() -> list[dict]:
    return db.q("SELECT * FROM customer WHERE (flags IS NULL OR flags NOT LIKE '%eval test customer%') ORDER BY id")


def _yearly(c: dict) -> float:
    f = c.get("fields") or {}
    price = f.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return 0.0
    period = str(f.get("period") or "").lower()
    return float(price) * 12 if period.startswith(("month", "μην")) else float(price)


def _amount(c: dict, goal: str) -> float:
    f = c.get("fields") or {}
    if goal == "collect":
        v = f.get("amount_due")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    return _yearly(c)


def _due(c: dict, goal: str, days: int, today: date):
    """-> (days until the date or since it, in_window) for this goal."""
    f = c.get("fields") or {}
    if goal == "collect":
        due = (f.get("amount_due") or 0) > 0
        d = _date(f.get("due_date") or f.get("expiry_date"))
        return ((d - today).days if d else None), due
    d = _date(f.get(_DATE_KEY[goal]))
    if not d:
        return None, False
    delta = (d - today).days
    if goal == "win_back":
        return -delta, -delta >= LAPSED_DAYS
    return delta, 0 <= delta <= days


def suggest_goal(days: int = DEFAULT_DAYS) -> dict:
    """Goal from the data: counts of customers that fit each goal template; the biggest fit wins (ties: renew first)."""
    db.init()
    today = db.now().date()
    cs = _customers()
    counts, eur = {}, {}
    for g in GOAL_ORDER:
        hit = [c for c in cs if _due(c, g, days, today)[1]]
        counts[g], eur[g] = len(hit), round(sum(_amount(c, g) for c in hit), 2)
    if not cs or not any(counts.values()):
        have = {k for c in cs for k, v in (c.get("fields") or {}).items() if v not in (None, "")}
        for g in GOAL_ORDER:
            if _DATE_KEY[g] in have or (g == "collect" and "amount_due" in have):
                return {"goal_type": g, "reason": f"the file has {_DATE_KEY[g]}, but nobody is due in the window",
                        "goal_text": _goal_text(g, days), "counts": counts, "eur_at_stake": 0, "days": days}
        return {"goal_type": None, "reason": "no date or amount column in the customer file: tell me the goal in your own words",
                "goal_text": "", "counts": counts, "eur_at_stake": 0, "days": days}
    best = max(GOAL_ORDER, key=lambda g: (counts[g], -GOAL_ORDER.index(g)))
    reason = _why(best, counts[best], days)
    return {"goal_type": best, "reason": f"{reason} (about {eur[best]:g} EUR)", "goal_text": _goal_text(best, days),
            "counts": counts, "eur_at_stake": eur[best], "days": days}


def _why(goal: str, n: int, days: int) -> str:
    return {"renew": f"{n} contracts end within {days} days",
            "collect": f"{n} customers owe money",
            "remind": f"{n} customers have something due within {days} days",
            "win_back": f"{n} customers have been inactive for {LAPSED_DAYS}+ days"}[goal]


def _goal_text(goal: str, days: int) -> str:
    lang = (db.company() or {}).get("language") or "el"
    t = _TEXT[goal]
    return t.get(lang, t["en"]).format(days=days, lapsed=LAPSED_DAYS)


def _lane(c: dict) -> dict:
    try:
        return compliance.lane(c)
    except Exception as e:  # noqa: BLE001  a lane that cannot be computed is a hold
        return {"lane": "hold", "rule": "lane_error", "reason": f"{type(e).__name__}", "next": "none"}


def radar(goal=None, days: int = DEFAULT_DAYS) -> dict:
    """Who is due, how much is at stake, and what we may do about each one. `goal` is a goal type (None = suggested)."""
    db.init()
    sug = None
    if not goal:
        sug = suggest_goal(days)
        goal = sug["goal_type"]
    if goal not in template.GOALS:
        return {"goal": goal, "days": days, "due": [], "eur_at_stake": 0, "lanes": {}, "error": "no goal: tell me what you want to achieve"}
    today = db.now().date()
    due, lanes = [], {}
    for c in _customers():
        delta, ok = _due(c, goal, days, today)
        if not ok:
            continue
        ln = _lane(c)
        eur = round(_amount(c, goal), 2)
        reason = f"{'inactive for' if goal == 'win_back' else 'in'} {abs(delta)} days" if delta is not None else "due"
        due.append({"customer_id": c["id"], "name": c["name"], "product": (c.get("fields") or {}).get("product"), "date": (c.get("fields") or {}).get(_DATE_KEY[goal]),
                    "days": delta, "eur": eur, "lane": ln.get("lane"), "lane_rule": ln.get("rule"), "lane_reason": ln.get("reason"),
                    "next": ln.get("next"), "reason": reason})
        s = lanes.setdefault(ln.get("lane") or "hold", {"customers": 0, "eur": 0.0})
        s["customers"] += 1
        s["eur"] = round(s["eur"] + eur, 2)
    due.sort(key=lambda r: (r["days"] if r["days"] is not None else 9999, -r["eur"]))
    return {"goal": goal, "goal_label": template.GOALS[goal]["label"], "days": days, "due": due,
            "eur_at_stake": round(sum(r["eur"] for r in due), 2), "lanes": lanes,
            "eur_basis": "yearly value from the price on file (monthly prices x12)" if goal != "collect" else "amount due",
            **({"suggested": sug} if sug else {})}


def brief() -> dict:
    """The owner's one-screen summary."""
    db.init()
    rd = radar()
    try:
        summary = compliance.lanes_summary()
    except Exception:  # noqa: BLE001
        summary = {}
    try:
        from prova import setup_agent
        qs = setup_agent.questions()
    except Exception:  # noqa: BLE001
        qs = []
    ai = rd.get("lanes", {}).get("ai_call", {"customers": 0, "eur": 0})
    other = rd.get("eur_at_stake", 0) - ai["eur"]
    head = (f"{len(rd.get('due', []))} customers are due ({rd.get('eur_at_stake', 0):g} EUR): {ai['customers']} the AI desk may call now "
            f"({ai['eur']:g} EUR), {other:g} EUR waits for consent or a human") if rd.get("due") else "nobody is due in the window"
    return {"goal": rd.get("goal"), "headline": head, "radar": {k: rd[k] for k in ("goal", "days", "eur_at_stake", "lanes") if k in rd},
            "top": rd.get("due", [])[:5], "lanes": summary, "open_questions": [{"id": q["id"], "fact": q["fact"]} for q in qs]}


# ------------------------------------------------------------------ several goals

ACTIVE_KEY = "goals_active"


def selected_goals() -> list[str]:
    """Only an explicit selection (set_goals); [] when nobody chose."""
    db.init()
    r = db.one("SELECT value FROM settings WHERE key=?", (ACTIVE_KEY,))
    v = r["value"] if r else None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    return [g for g in v if g in template.GOALS] if isinstance(v, list) else []


def active_goals() -> list[str]:
    """The goals the desk works on now: the owner's selection, else the company goal, else none."""
    got = selected_goals()
    if got:
        return got
    g = (db.company() or {}).get("goal_type")
    return [g] if g in template.GOALS else []


def propose_goals(days: int = DEFAULT_DAYS) -> list[dict]:
    """Every goal the customer file supports, computed from the data, best first (the first one is the default).
    [{goal, label, why, due_count, ai_call_count, eur_at_stake, needs_ok, active}]. needs_ok: the owner has not switched
    this goal on yet, so it needs their yes before any call runs for it."""
    db.init()
    today = db.now().date()
    cs = _customers()
    on = set(active_goals())
    out = []
    for g in GOAL_ORDER:
        hit = [c for c in cs if _due(c, g, days, today)[1]]
        if not hit:
            continue
        ai = sum(1 for c in hit if _lane(c).get("lane") == "ai_call")
        eur = round(sum(_amount(c, g) for c in hit), 2)
        out.append({"goal": g, "label": template.GOALS[g]["label"], "why": f"{_why(g, len(hit), days)} (about {eur:g} EUR; {ai} of them the AI desk may call now)",
                    "due_count": len(hit), "ai_call_count": ai, "eur_at_stake": eur, "needs_ok": g not in on, "active": g in on})
    out.sort(key=lambda p: (-p["due_count"], GOAL_ORDER.index(p["goal"])))
    return out


def set_goals(selected, by: str = "owner") -> dict:
    """Switch on 1..n goals (settings goals_active). The first is the company goal; allowed_purposes gets them all.
    An agent's selection is stored as 'filled' (the owner still confirms the slot); the owner's as 'confirmed'."""
    db.init()
    if isinstance(selected, str):
        selected = [selected]
    sel = []
    for g in selected or []:
        if g not in template.GOALS:
            return {"error": f"unknown goal {g}: choose from {list(template.GOALS)}"}
        if g not in sel:
            sel.append(g)
    if not sel:
        return {"error": "choose at least one goal"}
    db.q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (ACTIVE_KEY, json.dumps(sel)))
    if (db.company() or {}).get("goal_type") not in sel:
        db.update("company", 1, goal_type=sel[0], goal_text=_goal_text(sel[0], DEFAULT_DAYS))
    cur = db.slots().get("allowed_purposes") or []
    want = sel + [g for g in cur if g not in sel and g in template.GOALS and g in sel]
    if set(cur) != set(want) or not cur:
        db.set_slot("allowed_purposes", want, "goals chosen by the owner" if by == "owner" else f"goals proposed by {by}",
                    "confirmed" if by == "owner" else "filled")
    return {"goals": sel, "allowed_purposes": db.slots().get("allowed_purposes"), "primary": (db.company() or {}).get("goal_type"), "by": by}
