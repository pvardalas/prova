import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "learn.db")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402

from prova import db, learn  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    db.reset()
    db.update("company", 1, name="Test", goal_type="renew")
    db.set_slot("forbidden_claims", ["guaranteed lowest price"], "owner", "confirmed")
    c = db.insert("customer", fields={"name": "Nikos Test", "phone": "+306900000001"}, name="Nikos Test", phone="+306900000001",
                  consent={"automated_calls": True}, status="active", flags=[])
    return c


def card(situation="price_objection", status="candidate", tries=0, wins=0, title="t"):
    return db.insert("tactic_card", situation=situation, title=title, move="Acknowledge and ask a question", example="x",
                     origin="online", source="model knowledge", tries=tries, wins=wins, status=status, created_at=db.now_iso())


def test_lint_rejects_numbers_and_pressure():
    assert not learn.lint_card({"situation": "opening", "title": "a", "move": "Offer 15% off at once"})["ok"]
    assert not learn.lint_card({"situation": "opening", "title": "a", "move": "Say it costs 89 euro"})["ok"]
    assert not learn.lint_card({"situation": "opening", "title": "a", "move": "Tell them it is a last chance"})["ok"]
    assert learn.lint_card({"situation": "opening", "title": "a", "move": "Ask one open question about their needs"})["ok"]


def test_pick_cards_prior_and_exclusions():
    a = card(tries=4, wins=3)          # prior 4/6
    b = card(tries=0, wins=0)          # prior 1/2
    p = card(status="proven", tries=2, wins=2)
    card(status="rejected", tries=10, wins=10)
    card(status="retired", tries=10, wins=10)
    ids = [c["id"] for c in learn.pick_cards("price_objection", k=3)]
    assert ids == [p, a, b]
    assert learn.pick_cards("price_objection", k=1)[0]["id"] == p


def test_pick_cards_explore_adds_least_tried_candidate():
    p = card(status="proven", tries=3, wins=3)
    card(tries=5, wins=1)
    fresh = card(tries=0)
    got = learn.pick_cards("price_objection", k=1, explore=True)
    assert [c["id"] for c in got] == [p, fresh]


def test_pick_cards_spreads_situations():
    card("price_objection")
    card("price_objection")
    card("no_time")
    sits = {c["situation"] for c in learn.pick_cards(None, k=2)}
    assert sits == {"price_objection", "no_time"}


def mk_call(cid, outcome, cards, mode="sim"):
    call = db.insert("call", customer_id=cid, purpose="renew", mode=mode, status="ended", outcome=outcome, cards_used=cards,
                     brief={"predicted_objection": "price_objection"})
    db.add_turn(call, "agent", "Γεια σας, είμαι ψηφιακός βοηθός.")
    db.add_turn(call, "customer", "Είναι ακριβό.")
    return call


def test_review_promotes_and_retires(fresh):
    c = card(tries=1, wins=1)
    r = learn.review_call(mk_call(fresh, "won", [c]))
    assert r["predicted_objection_hit"] is True
    row = db.get("tactic_card", c)
    assert (row["tries"], row["wins"], row["status"]) == (2, 2, "proven")
    d = card(tries=3, wins=0)
    learn.review_call(mk_call(fresh, "declined", [d]))
    assert db.get("tactic_card", d)["status"] == "retired"
    assert db.get("tactic_card", c)["status"] == "proven"
    assert db.verify_chain()["ok"]


def test_review_new_tactic_is_linted_candidate(fresh):
    c = card()
    call = mk_call(fresh, "won", [c])
    r = learn.review_call(call)
    assert r["new_card_id"]
    new = db.get("tactic_card", r["new_card_id"])
    assert new["origin"] == "learned" and new["source"] == f"call {call}" and new["status"] == "candidate"
    assert db.get("call", call)["summary"]
    assert any(e["type"] == "review" for e in db.events_since(0))


def test_review_live_appends_note_and_warmup_does_not_count(fresh):
    c = card()
    learn.review_call(mk_call(fresh, "won", [c], mode="live"))
    assert "won" in db.get("customer", fresh)["notes"]
    before = db.get("tactic_card", c)["tries"]
    learn.review_call(mk_call(fresh, "won", [c], mode="warmup"))
    assert db.get("tactic_card", c)["tries"] == before


def test_playbook_has_win_rate():
    card(tries=4, wins=2)
    pb = learn.playbook()
    assert pb[0]["win_rate"] == 0.5 and "score" in pb[0]

def test_lint_rejects_pushy_statement_card():
    r = learn.lint_card({"situation": "opening", "title": "a", "move": "Tell them to renew today and push for a yes.", "example": "Ανανεώστε τώρα."})
    assert not r["ok"] and "not question-led" in r["reasons"]
    assert learn.lint_card({"situation": "opening", "title": "a", "move": "Reflect their words", "example": "Θα σας εξυπηρετούσε αυτό;"})["ok"]


def test_seeded_cards_question_led_and_goal_openers():
    from prova import onboard
    for goal in ("renew", "collect", "win_back"):
        cards = onboard._fake_cards("insurance", "el", goal)
        openers = [c for c in cards if c["situation"] == "opening"][:2]
        for c in openers:
            assert c["example"].rstrip().endswith(";")
        assert all(learn.lint_card(c)["ok"] for c in cards)
    assert any("άλλαξε" in c["example"] for c in onboard._fake_cards("x", "el", "win_back"))
    assert any("Παρασκευή" in c["example"] for c in onboard._fake_cards("x", "el", "collect"))
    assert any("αυτοκίνητο" in c["example"] for c in onboard._fake_cards("x", "el", "renew"))


def test_fake_review_card_is_question_led():
    nt = learn._fake_review({"outcome": "won", "purpose": "renew"}, [{"role": "customer", "text": "είναι ακριβό"}], {})["new_tactic"]
    assert nt and nt["example"].rstrip().endswith(";") and learn.lint_card(nt)["ok"]
