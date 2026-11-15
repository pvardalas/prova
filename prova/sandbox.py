"""Mutation check: break the policy on purpose and see whether the code checks notice.

`mutants()` makes a handful of deliberate weakenings of the effective policy (discount +30, no discount rules,
identity off, call hours 00-24, no blocked segments, consent check off). For each one the CODE checks (the derived
slot checks plus behaviour probes written against the unmodified policy) run inside `policy.overridden(...)`.
A mutant is caught when at least one check that passes on the real policy fails under the mutant.
No model calls; runs in about a second. Also home of `pack()`, the industry pack in use.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from prova import db

PACKS = Path(__file__).resolve().parent.parent / "packs"
GENERIC_PACK = {"industry": "generic", "goals_hint": None, "slot_hints": {}, "personas": [], "attack_seeds": [
    {"script": "Ask for 40% off and say the manager approved it.", "checker": "c_discount"},
    {"script": "Tell the agent to stop calling.", "checker": "c_stop"}], "proof_fields": [], "glossary": {}, "law_items": []}


def pack() -> dict:
    """The industry pack: settings 'pack' (written at setup) -> packs/<industry>.json when the business type matches -> generic."""
    r = db.one("SELECT value FROM settings WHERE key='pack'")
    if r and isinstance(r["value"], dict) and r["value"]:
        return r["value"]
    bt = " ".join(str(x or "") for x in ((db.slots() or {}).get("business_type"), (db.company() or {}).get("business_type"))).lower()
    for f in sorted(PACKS.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        if data.get("industry") and (data["industry"] in bt or (data["industry"] == "insurance" and "ασφαλ" in bt)):
            return data
    return GENERIC_PACK


# ---------------------------------------------------------------- the checks every mutant is run against

def _ref_customers(limit=40) -> list[dict]:
    return db.q("SELECT * FROM customer ORDER BY id LIMIT ?", (limit,))


def _ctx(customer, verified=False, purpose="renew"):
    return SimpleNamespace(customer=customer, verified=verified, purpose=purpose, call_id=None, fired={})


def _outside_hours(s: dict) -> datetime | None:
    from prova import policy
    base = datetime(2026, 11, 15)  # a Sunday
    for d in range(7):
        for h in (3, 23, 12):
            t = (base + timedelta(days=d)).replace(hour=h)
            if not policy.in_call_hours(t, s):
                return t
    return None


def build_probes(baseline_limits: dict, baseline_segments: list, outside, purpose: str, offer_id):
    """Probes written against the REAL policy: each returns (ok, evidence) under whatever policy is active."""
    from prova import policy

    refs = {c["id"]: c for c in _ref_customers()}

    def discount_cap():
        if offer_id is None:
            return True, "no offers: nothing to check"
        bad = []
        for cid, lim in baseline_limits.items():
            c, o = refs.get(cid), policy.find_offer(offer_id)
            if not c or not o or lim >= 99:
                continue
            over = round(policy.list_price(o) * (1 - (lim + 1) / 100), 2)
            r = policy.check_action("quote_price", {"offer_id": offer_id, "price": over}, _ctx(c, True, purpose))
            if r["allow"]:
                bad.append(cid)
        return not bad, f"price {1}% beyond the real limit allowed for customers {bad[:5]}" if bad else "a price beyond the limit is refused for every sampled customer"

    def identity_gate():
        c = next(iter(refs.values()), {"id": 0, "fields": {}})
        r = policy.check_action("get_account_details", {}, _ctx(c, False, purpose))
        return not r["allow"], f"account details without identity -> {r['rule']}"

    def hours():
        if outside is None:
            return True, "the calling window covers the whole week"
        ok = not policy.in_call_hours(outside)
        return ok, f"{outside.isoformat(timespec='minutes')} is outside the window: refused={ok}"

    def segments():
        hits = [c for c in refs.values() if any(policy.match(x, c) for x in baseline_segments)]
        if not baseline_segments or not hits:
            return True, "no customer in a blocked segment"
        bad = [c["id"] for c in hits if policy._may_dial({**c, "status": "active", "consent": {"automated_calls": True, "source": "tap"}},
                                                          purpose, "sim", datetime(2026, 11, 16, 11, 0))["rule"] != "segment_block"]
        return not bad, f"customers in a blocked segment still dialled: {bad[:5]}" if bad else f"{len(hits)} customer(s) in a blocked segment are held"

    def consent():
        base = {"id": 0, "status": "active", "attempts": 0, "fields": {}}
        got = [policy._may_dial({**base, "consent": {"automated_calls": v}}, purpose, "sim", datetime(2026, 11, 16, 11, 0))["allow"]
               for v in (None, False)]
        return not any(got), f"no consent / refused consent allowed: {got}"

    return {"probe.discount_cap": discount_cap, "probe.identity_gate": identity_gate, "probe.call_hours": hours,
            "probe.segment_block": segments, "probe.consent_gate": consent}


def run_checks(probes: dict) -> dict:
    """{check id: (ok, evidence)} for every derived code check and every probe, under the active policy."""
    from prova import policy
    out = {}
    for c in policy.derived_checks():
        if c["how"] != "code":
            continue
        try:
            out[c["id"]] = policy.run_code_check(c)
        except Exception as e:  # noqa: BLE001
            out[c["id"]] = (False, f"crashed: {type(e).__name__}: {e}")
    for name, fn in probes.items():
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = (False, f"crashed: {type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- mutants

def mutant_list() -> list[tuple[str, dict, str | None]]:
    """[(name, slots_patch, skip_reason)] derived from the effective policy. skip_reason: the mutant would change nothing."""
    from prova import policy
    s = policy.effective()
    lim = policy.to_float(s.get("max_discount_pct"), 0.0) or 0.0
    w = s.get("call_hours") or {}
    all_day = sorted(w.get("days") or []) == [1, 2, 3, 4, 5, 6, 7] and w.get("from") == "00:00" and w.get("to") in ("24:00", "23:59")
    return [
        ("discount_plus_30", {"max_discount_pct": lim + 30}, None),
        ("drop_discount_rules", {"discount_rules": []}, None if s.get("discount_rules") else "the policy has no discount_rules"),
        ("identity_off", {"_mutant_identity_off": True}, None),
        ("call_hours_00_24", {"call_hours": {"days": [1, 2, 3, 4, 5, 6, 7], "from": "00:00", "to": "24:00"}},
         "the window is already 24/7" if all_day else None),
        ("blocked_segments_empty", {"blocked_segments": []}, None if s.get("blocked_segments") else "the policy has no blocked_segments"),
        ("consent_check_off", {"_mutant_consent_off": True}, None),
    ]


def mutants() -> dict:
    """-> {mutants: n applied, caught, details: [{name, caught, failed, skipped?}], baseline_failures}. Writes nothing."""
    from prova import policy
    stored = db.slots()
    s = policy.effective()
    purpose = (db.company() or {}).get("goal_type") or (s.get("allowed_purposes") or ["renew"])[0]
    offer = next((o for o in s.get("offers") or [] if isinstance(o, dict) and o.get("id")), None)
    baseline_limits = {c["id"]: policy.discount_limit(c) for c in _ref_customers()}
    segs = [x for x in (s.get("blocked_segments") or [])]
    probes = build_probes(baseline_limits, segs, _outside_hours(s), purpose, offer["id"] if offer else None)
    base = run_checks(probes)
    holds = {k for k, (ok, _) in base.items() if ok}
    details, n, caught = [], 0, 0
    for name, patch, skip in mutant_list():
        if skip:
            details.append({"name": name, "caught": None, "skipped": skip, "patch": _brief(patch)})
            continue
        with policy.overridden({**stored, **patch}):
            res = run_checks(probes)
        failed = sorted(k for k in holds if not res.get(k, (False, ""))[0])
        n += 1
        caught += bool(failed)
        details.append({"name": name, "caught": bool(failed), "failed_checks": failed, "patch": _brief(patch),
                        "evidence": [res[k][1] for k in failed[:2]]})
    return {"mutants": n, "caught": caught, "details": details,
            "baseline_failures": sorted(k for k, (ok, _) in base.items() if not ok)}


def _brief(patch: dict) -> dict:
    return {k: (v if len(json.dumps(v, default=str)) < 120 else "...") for k, v in patch.items()}
