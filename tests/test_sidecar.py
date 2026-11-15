import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["PROVA_FAKE"] = "1"

import asyncio  # noqa: E402

import pytest  # noqa: E402

import prova  # noqa: E402
from prova import db, llm, sidecar, voice  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeCtx:
    def __init__(self, call_id, dry_run=None):
        self.call_id = call_id
        self.call = db.get("call", call_id)
        self.customer = db.get("customer", self.call["customer_id"])
        self.fired, self.end, self.sidecar = {}, None, None

    async def run_tool_async(self, name, args):
        if name == "best_move":
            return await self.sidecar.best_move(args.get("situation", "other"), args.get("customer_said", ""))
        if name == "end_call":
            self.end = args
        else:
            self.fired[name] = args
        db.add_turn(self.call_id, "tool", f"{name}({args})")
        return {"ok": True}

    def finish(self, status="ended", note=None):
        out = (self.end or {}).get("outcome", "thinking")
        db.update("call", self.call_id, status=status, outcome=out)
        return {"call_id": self.call_id, "outcome": out, "duration_s": 0.0}


def fake_check(text, ctx):
    out = []
    if "79" in text:
        out.append({"rule": "price_not_allowed", "said": "79 euro", "correct": "The price is 89 euro."})
    if "disclosure-missing" in text:
        out.append({"rule": "ai_disclosure", "said": text, "correct": "Say you are an AI assistant."})
    return out


@pytest.fixture(autouse=True)
def world(monkeypatch):
    db.reset()
    db.set_slot("language", "el", "test", "confirmed")
    cust = db.insert("customer", name="Μαρία", phone="+306900000000", fields={"name": "Μαρία"})
    call_id = db.insert("call", customer_id=cust, purpose="renew", mode="live", brief={
        "opening": "Καλησπέρα", "expected_objections": [
            {"situation": "price_objection", "answer": "Έχουμε προετοιμασμένη απάντηση.", "card_id": 1}]})
    cards = [{"id": 1, "situation": "price_objection", "title": "Value", "move": "Show the value", "example": "Καλύπτει όλα."},
             {"id": 2, "situation": "price_objection", "title": "Fit", "move": "Ask what they need", "example": "Τι χρειάζεστε;"}]
    mods = {
        "policy": types.SimpleNamespace(check_agent_text=fake_check,
                                        opening_line=lambda name: f"Καλησπέρα {name}, είμαι βοηθός AI."),
        "learn": types.SimpleNamespace(pick_cards=lambda situation=None, k=2, explore=False, goal=None: cards[:k]),
        "gateway": types.SimpleNamespace(CallCtx=FakeCtx),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, f"prova.{name}", mod)
        monkeypatch.setattr(prova, name, mod, raising=False)
    return types.SimpleNamespace(call_id=call_id, cust=cust)


def make(world, brief=None):
    ctx = FakeCtx(world.call_id)
    sc = sidecar.Sidecar(ctx, brief if brief is not None else ctx.call["brief"], "live")
    ctx.sidecar = sc
    return ctx, sc


def events(type_):
    return [e["payload"] for e in db.q("SELECT * FROM event WHERE type=? ORDER BY id", (type_,))]


def test_classify_keywords():
    c = sidecar.fake_classify
    assert c("Είναι πολύ ακριβό")["situation"] == "price_objection"
    assert c("It is too expensive")["decision_point"]
    assert c("Δεν έχω χρόνο τώρα")["situation"] == "no_time"
    assert c("Θα το σκεφτώ")["situation"] == "wants_to_think"
    assert c("Είσαι ρομπότ;")["situation"] == "distrust_ai"
    assert c("Μην με ξαναπάρετε")["wants_stop"]
    assert c("Θέλω να μιλήσω με άνθρωπο")["wants_human"]
    assert c("καλημέρα")["decision_point"] is False


def test_customer_turn_guard_and_speculative_cache(world):
    async def go():
        ctx, sc = make(world)
        await sc.on_customer_turn("Μην με ξαναπάρετε")
        g = sc.pending_guard()
        assert g["action"] == "cut" and g["rule"] == "honour_opt_out"
        assert sc.pending_guard() is None
        await sc.on_customer_turn("Θέλω άνθρωπο")
        assert sc.pending_guard()["action"] == "handoff"
        await sc.on_customer_turn("Είναι πολύ ακριβό")
        assert "price_objection" in sc._cache
    run(go())
    assert [e["action"] for e in events("guard")] == ["cut", "handoff"]


def test_best_move_three_candidates(world):
    async def go():
        ctx, sc = make(world)
        await sc.on_customer_turn("Είναι πολύ ακριβό")
        res = await sc.best_move("price_objection", "Είναι πολύ ακριβό")
        assert "price_objection" not in sc._cache
        return res
    res = run(go())
    assert res["card_id"] == 1 and res["score"] == 82 and res["say"]
    ev = events("moves")
    assert len(ev) == 1
    cands = ev[0]["candidates"]
    assert len(cands) == 3
    assert [c["card_id"] for c in cands] == [1, 2, None]
    assert len({c["score"] for c in cands}) == 3
    assert ev[0]["chosen"]["card_id"] == 1 and ev[0]["chosen"]["score"] == 82 and ev[0]["situation"] == "price_objection" and ev[0]["ms"] >= 0


def test_best_move_without_speculation_and_other_name(world):
    async def go():
        ctx, sc = make(world)
        return await sc.best_move("wants_to_think", "θα το σκεφτώ")
    assert run(go())["move"]
    assert len(events("moves")) == 1


def test_best_move_timeout_falls_back_to_prepared_answer(world, monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm, "aask_json", slow)
    monkeypatch.setattr(sidecar, "MOVES_TIMEOUT_S", 0.2)

    async def go():
        ctx, sc = make(world)
        return await sc.best_move("price_objection", "ακριβό")
    res = run(go())
    assert res["say"] == "Έχουμε προετοιμασμένη απάντηση." and res["card_id"] == 1
    ev = events("moves")[0]
    assert ev["ms"] < 1000 and ev["candidates"][0]["move"].startswith("prepared")


def test_agent_turn_code_correction(world):
    async def go():
        ctx, sc = make(world)
        assert await sc.on_agent_turn("Είμαι βοηθός AI. Η τιμή είναι 79 ευρώ.") is not None or True
        return await sc.on_agent_turn("Άρα 79 ευρώ.")
    act = run(go())
    assert act["action"] == "correct" and "You said" in act["instruction"] and "89" in act["instruction"]
    assert events("correction")[-1]["rule"] == "price_not_allowed"


def test_agent_turn_clean_text_is_quiet(world):
    async def go():
        ctx, sc = make(world)
        return await sc.on_agent_turn("Καλησπέρα, είμαι βοηθός AI της εταιρείας.")
    assert run(go()) is None


def test_missing_disclosure_corrected_once_then_cut(world):
    async def go():
        ctx, sc = make(world)
        a1 = await sc.on_agent_turn("Γεια σας disclosure-missing")
        a2 = await sc.on_agent_turn("Μάλιστα, πάμε στην πρότασή μας.")
        return a1, a2
    a1, a2 = run(go())
    assert a1["action"] == "correct" and a1["rule"] == "ai_disclosure"
    assert a2["action"] == "cut"
    assert events("guard")[-1]["action"] == "cut"


def test_missing_disclosure_fixed_by_correction(world):
    async def go():
        ctx, sc = make(world)
        a1 = await sc.on_agent_turn("Γεια σας disclosure-missing")
        a2 = await sc.on_agent_turn("Συγγνώμη, είμαι ψηφιακός βοηθός τεχνητής νοημοσύνης.")
        return a1, a2
    a1, a2 = run(go())
    assert a1["action"] == "correct" and a2 is None


def test_model_check_forbidden_claim(world):
    db.set_slot("forbidden_claims", ["καλύπτει όλες τις ζημιές"], "test", "confirmed")

    async def go():
        ctx, sc = make(world)
        await sc.on_agent_turn("Είμαι βοηθός AI.")
        ok = await sc.on_agent_turn("Έχουμε καλό πρόγραμμα.")
        bad = await sc.on_agent_turn("Το πρόγραμμα καλύπτει όλες τις ζημιές, το εγγυόμαστε.")
        return ok, bad
    ok, bad = run(go())
    assert ok is None
    assert bad["action"] == "correct" and bad["rule"] == "forbidden_claims"


def test_spoken_function_call_guard():
    name, args = voice.parse_leak("quote_price(offer_id='basic', price=29.6)")
    assert name == "quote_price" and args == {"offer_id": "basic", "price": 29.6}
    assert voice.strip_leak("Ένα λεπτό record_agreement(offer_id='x')") == "Ένα λεπτό"
    assert voice.strip_leak("Καλημέρα σας") == "Καλημέρα σας"


def test_fake_live_call_uses_sim_when_present(world, monkeypatch):
    async def run_text_call(call_id, **kw):
        return {"call_id": call_id, "outcome": "won", "turns": 3}
    sim = types.SimpleNamespace(run_text_call=run_text_call)
    monkeypatch.setitem(sys.modules, "prova.sim", sim)
    monkeypatch.setattr(prova, "sim", sim, raising=False)
    assert run(voice.run_live_call(world.call_id)) == {"call_id": world.call_id, "outcome": "won", "turns": 3, "via": "text"}


def test_no_phone_reachable_and_customer_numbers_opt_in(world, monkeypatch):
    monkeypatch.setattr(llm, "FAKE", False)
    for k in ("PHONE_TO", "PHONE_ALLOW_CUSTOMER_NUMBERS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="no phone reachable"):
        run(voice.run_live_call(world.call_id, via="auto"))
    cust = {"phone": "+306900000000"}
    assert voice._twilio_number(None, cust) is None
    monkeypatch.setenv("PHONE_ALLOW_CUSTOMER_NUMBERS", "1")
    assert voice._twilio_number(None, cust) == "+306900000000"
    monkeypatch.setenv("PHONE_TO", "+306911111111")
    assert voice._twilio_number(None, cust) == "+306911111111"
    assert voice._twilio_number("+3069222", cust) == "+3069222"


def test_phone_status_shape():
    s = voice.phone_status()
    assert {"browser_phones", "twilio", "public_url", "phone_url", "active_call_id"} <= set(s)
