import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "gateway.db")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402

from prova import db, gateway  # noqa: E402

OFFERS = [{"id": "basic", "name": "Βασικό", "price": 100, "period": "year"},
          {"id": "gold", "name": "Gold", "price": 200, "period": "year",
           "eligibility": {"field": "tenure_years", "op": ">=", "value": 5}}]


@pytest.fixture(autouse=True)
def fresh():
    db.reset()
    for k, v in {"company_name": "Ασφάλειες Παπαδάκης", "business_type": "broker", "language": "el", "offers": OFFERS,
                 "handoff_contact": "Νίκος 2101234567", "max_discount_pct": 10, "allowed_purposes": ["renew", "book", "collect"],
                 "identity_check": {"field": "expiry_date", "question": "Πότε λήγει;"}}.items():
        db.set_slot(k, v, "test", "confirmed")
    db.update("company", 1, status="live", goal_type="renew")
    db.set_clock("2026-11-16T10:30")


def make(mode="sim", purpose="renew", **fields):
    f = {"name": "Γιώργος Παπαδόπουλος", "since_date": "2024-01-01", "expiry_date": "2026-12-20", "price": 95,
         "amount_due": 50, "notes": "secret", "extra": {"x": 1}}
    f.update(fields)
    cid = db.insert("customer", name=f["name"], fields=f, consent={"automated_calls": True}, status="active")
    call = db.insert("call", customer_id=cid, purpose=purpose, mode=mode, status="live")
    return cid, call, gateway.CallCtx(call)


def test_tools_for():
    names = lambda p: {t["name"] for t in gateway.tools_for(p)}  # noqa: E731
    assert len(gateway.TOOLS) == 13
    assert "record_agreement" in names("renew") and "record_booking" not in names("renew")
    assert "record_promise" in names("collect") and "record_agreement" not in names("collect")
    assert {"get_offers", "end_call", "best_move", "mark_do_not_call"} <= names("remind")
    assert gateway.live_tools("renew")[0]["function_declarations"]


def test_dry_run_default():
    assert make("sim")[2].dry_run and make("eval")[2].dry_run and not make("live")[2].dry_run
    assert not gateway.CallCtx(make("sim")[1], dry_run=False).dry_run


@pytest.mark.parametrize("answer,ok", [
    ("20/12/2026", True), ("2026", True), ("20 Δεκεμβρίου 2026", True), ("20-12-2026", True), ("2026-12-20", True),
    ("21/12/2026", False), ("2025", False), ("12/2026", True), ("19 Δεκεμβρίου 2026", False), ("", False),
])
def test_identity_dates(answer, ok):
    assert gateway.loose_match(answer, "2026-12-20") is ok


def test_identity_text_and_numbers():
    assert gateway.loose_match("  γιωργος   ΠΑΠΑΔΟΠΟΥΛΟΣ", "Γιώργος Παπαδόπουλος")
    assert gateway.loose_match("Παπαδόπουλος", "Γιώργος Παπαδόπουλος")
    assert not gateway.loose_match("Νικολαΐδης", "Γιώργος Παπαδόπουλος")
    assert gateway.loose_match("1985", 1985) and not gateway.loose_match("1986", 1985)
    assert gateway.loose_match("69 12 34 56 78", "+306912345678") or gateway.loose_match("6912345678", "+306912345678")


def test_identity_gate_and_account_details():
    cid, call, ctx = make()
    r = ctx.run_tool("get_account_details", {})
    assert "error" in r and ctx.denied[0]["rule"] == "identity"
    bad = ctx.run_tool("verify_identity", {"answer": "2020"})
    assert bad["verified"] is False and "next" in bad and not ctx.verified
    ok = ctx.run_tool("verify_identity", {"answer": "20/12/2026"})
    assert ok["verified"] and ctx.verified
    acc = ctx.run_tool("get_account_details", {})
    assert acc["account"]["expiry_date"] == "2026-12-20" and "extra" not in acc["account"] and "next" in acc
    assert "expiry_date" in ctx.customer_view()


def test_two_failed_identity_attempts_end_it():
    _, _, ctx = make()
    ctx.run_tool("verify_identity", {"answer": "1999"})
    assert "end_call" in ctx.run_tool("verify_identity", {"answer": "1998"})["next"]


def test_get_offers_and_quote():
    _, _, ctx = make()
    offers = ctx.run_tool("get_offers", {})["offers"]
    assert [o["id"] for o in offers] == ["basic"]
    q = ctx.run_tool("quote_price", {"offer_id": "basic", "price": 92})
    assert q["allowed"] and q["discount_pct"] == 8 and "say" in q
    assert ctx.run_tool("quote_price", {"offer_id": "basic", "price": 120})["discount_pct"] == 0
    low = ctx.run_tool("quote_price", {"offer_id": "basic", "price": 80})
    assert "error" in low and "90.00" in low["hint"] and ctx.denied[-1]["rule"] == "max_discount_pct"
    assert "error" in ctx.run_tool("quote_price", {"offer_id": "gold", "price": 190})


def test_record_agreement_denied_then_allowed():
    _, call, ctx = make()
    r = ctx.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 5})
    assert "error" in r and ctx.rejected
    assert ctx.outcome() == "human"
    ctx.run_tool("verify_identity", {"answer": "2026"})
    r = ctx.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 25})
    assert "error" in r and "10" in r["hint"]
    ok = ctx.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 10})
    assert ok["status"] == "agreement_recorded" and ok["price"] == 90 and "confirm" in ok["next"]
    assert ctx.outcome() == "won"
    res = ctx.finish()
    assert res["outcome"] == "won" and res["outcome_detail"]["offer_id"] == "basic" and res["outcome_detail"]["price"] == 90
    row = db.get("call", call)
    assert row["outcome"] == "won" and row["status"] == "ended" and row["ended_at"]
    assert db.q("SELECT * FROM event WHERE type='call_ended' AND call_id=?", (call,))


def test_every_tool_call_writes_turn_and_event():
    _, call, ctx = make()
    ctx.run_tool("get_offers", {})
    ctx.run_tool("get_account_details", {})  # denied
    assert [t["role"] for t in db.turns(call)] == ["tool", "tool"]
    evs = [e["payload"] for e in db.q("SELECT * FROM event WHERE type='tool' ORDER BY id")]
    assert [(e["name"], e["allowed"]) for e in evs] == [("get_offers", True), ("get_account_details", False)]
    assert evs[1]["rule"] == "identity" and "result" in evs[1] and "args" in evs[1]


def test_dry_run_writes_nothing_outside_call():
    cid, call, ctx = make("sim")
    fu = db.insert("followup", customer_id=cid, due_at="2026-12-01T10:00:00", reason="callback", status="pending")
    ctx.run_tool("mark_do_not_call", {})
    ctx.run_tool("schedule_callback", {"when": "tomorrow 11:00", "note": "x"})
    ctx.run_tool("transfer_to_human", {"reason": "x"})
    ctx.run_tool("end_call", {"outcome": "do_not_call", "note": "stop"})
    assert ctx.outcome() == "do_not_call"
    ctx.finish()
    assert db.get("customer", cid)["status"] == "active" and db.get("customer", cid)["attempts"] == 0
    assert db.get("followup", fu)["status"] == "pending"
    assert len(db.q("SELECT * FROM followup")) == 1
    assert not db.q("SELECT * FROM handoff") and not db.q("SELECT * FROM confirmation")
    assert db.get("call", call)["outcome"] == "do_not_call"


def test_live_do_not_call_takes_effect():
    cid, call, ctx = make("live")
    fu = db.insert("followup", customer_id=cid, due_at="2026-12-01T10:00:00", reason="callback", status="pending")
    ctx.run_tool("mark_do_not_call", {})
    assert db.get("customer", cid)["status"] == "do_not_call" and db.get("followup", fu)["status"] == "cancelled"


def test_schedule_callback_keeps_the_time_asked():
    _, _, ctx = make()
    r = ctx.run_tool("schedule_callback", {"when": "2026-11-16 23:00", "note": "late"})  # no calling-hours rule
    assert "moved" not in r and r["when"] == "2026-11-16T23:00" and ctx.detail["callback_at"] == "2026-11-16T23:00"
    _, _, ctx2 = make()
    r = ctx2.run_tool("schedule_callback", {"when": "2026-11-17 15:00", "note": "ok"})
    assert not r.get("moved") and r["when"] == "2026-11-17T15:00"
    assert "error" in ctx2.run_tool("schedule_callback", {"when": "kapote", "note": "?"})
    assert ctx2.outcome() == "callback"


def test_booking_and_promise_goals():
    _, _, ctx = make(purpose="book")
    assert "error" in ctx.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 0})  # wrong tool for purpose
    assert "error" in ctx.run_tool("record_booking", {"when": "2026-11-01 10:00", "note": ""})
    assert ctx.run_tool("record_booking", {"when": "2026-11-18 10:00", "note": "visit"})["status"] == "booked"
    assert ctx.outcome() == "won"
    _, _, c2 = make(purpose="collect")
    c2.verified = True
    assert "error" in c2.run_tool("record_promise", {"amount": 80, "date": "2026-11-20"})
    assert c2.run_tool("record_promise", {"amount": 50, "date": "2026-11-20"})["status"] == "promise_recorded"
    assert c2.outcome() == "won"


def test_outcome_cases():
    _, _, c = make()
    assert c.outcome() == "thinking"
    c.run_tool("end_call", {"outcome": "declined", "note": "no"})
    assert c.outcome() == "declined"
    _, _, c = make()
    c.run_tool("end_call", {"outcome": "won", "note": "claims a sale but no tool fired"})
    assert c.outcome() == "thinking"
    _, _, c = make()
    c.run_tool("end_call", {"outcome": "human", "note": "wants a person"})
    assert c.outcome() == "human" and c.detail["reason"] == "wants a person"
    _, _, c = make()
    c.run_tool("transfer_to_human", {"reason": "complaint"})
    c.run_tool("schedule_callback", {"when": "tomorrow 11:00", "note": ""})
    assert c.outcome() == "human"  # human > callback
    c.run_tool("mark_do_not_call", {})
    assert c.outcome() == "do_not_call"  # do_not_call > human
    _, _, c = make()  # denied success attempt, then agent ends as 'renewed'
    c.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 50})
    c.run_tool("end_call", {"outcome": "won", "note": "agreed"})
    assert c.outcome() == "human" and "not booked" in c.detail["reason"]
    _, _, c = make()
    c.run_tool("record_agreement", {"offer_id": "basic", "discount_pct": 50})
    c.run_tool("end_call", {"outcome": "declined", "note": "they said no after all"})
    assert c.outcome() == "declined"


def test_finish_failed_status():
    _, call, c = make()
    assert c.finish("failed", "boom")["outcome"] == "failed"
    assert db.get("call", call)["status"] == "failed"


class FakeSidecar:
    async def best_move(self, situation, customer_said=""):
        return {"move": "empathise", "say": f"({situation}) καταλαβαίνω", "card_id": 3, "score": 80}


def test_best_move_async_and_fallback():
    _, call, ctx = make()
    ctx.sidecar = FakeSidecar()
    r = asyncio.run(ctx.run_tool_async("best_move", {"situation": "price_objection", "customer_said": "ακριβό"}))
    assert r["say"] == "(price_objection) καταλαβαίνω" and r["card_id"] == 3 and "next" in r
    assert db.q("SELECT * FROM event WHERE type='tool' AND call_id=?", (call,))
    ctx.sidecar = None
    db.update("call", call, brief={"expected_objections": [{"situation": "no_time", "answer": "Δύο λεπτά μόνο."}]})
    ctx.call = db.get("call", call)
    assert asyncio.run(ctx.run_tool_async("best_move", {"situation": "no_time"}))["say"] == "Δύο λεπτά μόνο."
    assert "next" in ctx.run_tool("best_move", {"situation": "other"})


def test_async_runs_normal_tools():
    _, _, ctx = make()
    assert asyncio.run(ctx.run_tool_async("get_offers", {}))["offers"]


def test_identity_by_extra_column_with_lookalike_letters():
    db.set_slot("identity_check", {"field": "Πινακίδα", "question": "Ποια η πινακίδα;"}, "test", "confirmed")
    cid, call, ctx = make(extra={"Πινακίδα": "ΑΧΒ-4412"})
    assert ctx.run_tool("verify_identity", {"answer": "axb 4412"})["verified"] is True
    cid, call, ctx = make(extra={"Πινακίδα": "ΑΧΒ-4412"})
    assert ctx.run_tool("verify_identity", {"answer": "Γιώργος Παπαδόπουλος"})["verified"] is False
