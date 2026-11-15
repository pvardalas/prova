import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "policy.db")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402

from prova import db, policy  # noqa: E402
from prova.template import FLOOR  # noqa: E402

OFFERS = [{"id": "basic", "name": "Βασικό", "price": 100, "period": "year", "details": "τρίτων"},
          {"id": "gold", "name": "Gold", "price": 200, "period": "year",
           "eligibility": {"field": "tenure_years", "op": ">=", "value": 5}}]


def seed():
    db.reset()
    for k, v in {"company_name": "Ασφάλειες Παπαδάκης", "business_type": "insurance broker", "language": "el",
                 "handoff_contact": "Νίκος 2101234567", "offers": OFFERS, "max_discount_pct": 15,
                 "discount_rules": [{"when": "customer for under 5 years", "field": "tenure_years", "op": "<", "value": 5,
                                     "max_discount_pct": 10}],
                 "allowed_purposes": ["renew"], "identity_check": {"field": "expiry_date", "question": "Πότε λήγει;"},
                 "confirmation": {"channel": "sms", "withdrawal_text": "14 ημέρες"}}.items():
        db.set_slot(k, v, "test", "confirmed")
    db.update("company", 1, status="live", goal_type="renew", budget_eur=5.0)
    db.set_clock("2026-11-16T10:30")  # a Monday


def customer(**kw):
    c = {"id": 1, "status": "active", "consent": {"automated_calls": True}, "attempts": 0, "last_called_at": None,
         "fields": {"name": "Γιώργος Παπαδόπουλος", "since_date": "2024-01-01", "price": 95, "expiry_date": "2026-12-20"}}
    c.update(kw)
    return c


@pytest.fixture(autouse=True)
def fresh():
    seed()


def test_effective_applies_floor():
    db.set_slot("max_attempts", 5000, "t")
    db.set_slot("min_gap_hours", 1, "t")
    e = policy.effective()
    assert "calling_hours" not in e
    assert e["max_attempts"] == FLOOR["max_attempts_cap"] and e["min_gap_hours"] == 1


def test_missing_required():
    keys = {m["key"] for m in policy.missing_required()}
    assert "recording" not in keys and "handoff_contact" not in keys
    db.q("UPDATE policy_slot SET status='empty' WHERE key='handoff_contact'")
    assert "handoff_contact" in {m["key"] for m in policy.missing_required()}


def test_may_dial_order_and_denials():
    c = customer()
    assert policy.may_dial(c, "renew")["allow"]
    assert db.q("SELECT * FROM event WHERE type='dial_check'")  # live mode emits
    db.update("company", 1, status="paused")
    assert policy.may_dial(c, "renew")["rule"] == "company_status"
    assert policy.may_dial(c, "renew", "sim")["allow"]  # non-live skips company status
    db.update("company", 1, status="live")
    assert policy.may_dial(c, "book")["rule"] == "allowed_purposes"
    assert policy.may_dial(customer(status="do_not_call"), "renew", "sim")["rule"] == "do_not_call"
    assert policy.may_dial(customer(status="won"), "renew")["rule"] == "customer_status"
    assert policy.may_dial(customer(consent={"automated_calls": None}), "renew", "eval")["rule"] == "consent"
    assert policy.may_dial(customer(consent=None), "renew")["rule"] == "consent"
    for when in ("2026-11-16T23:00", "2026-11-15T11:00", "2026-11-16T04:00"):  # late night, Sunday, before dawn
        db.set_clock(when)
        assert policy.may_dial(c, "renew")["rule"] == "call_hours", when  # live calls only inside the window
        assert policy.may_dial(c, "renew", "sim")["allow"], when          # sandbox modes have no hours rule
    db.set_clock("2026-11-16T10:30")
    assert policy.may_dial(customer(attempts=3), "renew")["rule"] == "max_attempts"
    assert policy.may_dial(customer(last_called_at=db.now_iso()), "renew")["rule"] == "min_gap"
    db.add_cost("llm", 6.0)
    assert policy.may_dial(c, "renew")["rule"] == "budget"
    assert policy.may_dial(c, "renew", "sim")["allow"]


def test_attempts_checked_before_gap():
    r = policy.may_dial(customer(attempts=3, last_called_at=db.now_iso()), "renew")
    assert r["rule"] == "max_attempts"


def test_discount_limit_rules_and_eligibility():
    assert policy.discount_limit(customer()) == 10
    old = customer(fields={"name": "x", "since_date": "2015-01-01"})
    assert policy.discount_limit(old) == 15
    assert policy.discount_limit(customer(fields={"name": "x"})) == 10  # unknown tenure: narrower applies
    assert [o["id"] for o in policy.eligible_offers(customer())] == ["basic"]
    assert [o["id"] for o in policy.eligible_offers(old)] == ["basic", "gold"]


def test_check_action_quote_and_agreement():
    ctx = SimpleNamespace(customer=customer(), verified=False, purpose="renew")
    assert policy.check_action("quote_price", {"offer_id": "basic", "price": 90}, ctx)["allow"]
    d = policy.check_action("quote_price", {"offer_id": "basic", "price": 85}, ctx)
    assert not d["allow"] and d["rule"] == "max_discount_pct" and "90.00" in d["hint"]
    assert policy.check_action("quote_price", {"offer_id": "gold", "price": 190}, ctx)["rule"] == "eligibility"
    assert policy.check_action("quote_price", {"offer_id": "nope", "price": 1}, ctx)["rule"] == "offers"
    assert policy.check_action("record_agreement", {"offer_id": "basic", "discount_pct": 5}, ctx)["rule"] == "identity"
    ctx.verified = True
    assert policy.check_action("record_agreement", {"offer_id": "basic", "discount_pct": 10}, ctx)["allow"]
    assert policy.check_action("record_agreement", {"offer_id": "basic", "discount_pct": 12}, ctx)["rule"] == "max_discount_pct"
    assert policy.check_action("record_booking", {"when": "tomorrow 10:00"}, ctx)["rule"] == "wrong_tool"


def test_check_action_other_tools_and_errors():
    ctx = SimpleNamespace(customer=customer(fields={"name": "x", "amount_due": 50}), verified=True, purpose="collect")
    assert policy.check_action("record_promise", {"amount": 60, "date": "2026-11-20"}, ctx)["rule"] == "amount"
    assert policy.check_action("record_promise", {"amount": 50, "date": "2026-11-10"}, ctx)["rule"] == "promise_date"
    assert policy.check_action("record_promise", {"amount": 50, "date": "2026-11-20"}, ctx)["allow"]
    b = SimpleNamespace(customer=customer(), verified=False, purpose="book")
    assert policy.check_action("record_booking", {"when": "2026-11-10 10:00"}, b)["rule"] == "booking_time"
    assert policy.check_action("record_booking", {"when": "2026-11-17 10:00"}, b)["allow"]
    assert policy.check_action("get_account_details", {}, b)["rule"] == "identity"
    assert policy.check_action("mark_do_not_call", {}, b)["allow"]
    assert policy.check_action("hack", {}, b)["rule"] == "unknown_tool"
    assert policy.check_action("quote_price", {"offer_id": "basic", "price": "abc"}, b)["allow"] is False
    assert policy.check_action("quote_price", {"offer_id": "basic"}, None)["allow"] is False  # exception -> deny


def test_opening_line():
    el = policy.opening_line("Γιώργος")
    assert "Ασφάλειες Παπαδάκης" in el and "AI assistant" in el and el.endswith("Μιλάω με Γιώργος;")
    assert "καταγράφεται" in policy.recording_line() and "καταγρ" not in el
    assert "αντιταχθείτε" not in el and "αντιταχθείτε" in policy.opening_followup("el") and len(el) < 160
    db.set_slot("recording", {"on": False, "notice": None}, "t")
    assert policy.recording_line() == ""
    db.set_slot("language", "en", "t")
    db.set_slot("recording", {"on": True, "notice": None}, "t")
    en = policy.opening_line("George")
    assert "AI assistant" in en and "recorded" in policy.recording_line()
    db.set_slot("language", "de", "t")
    assert "AI assistant" in policy.opening_line("X")


def texts(t, first=False, cust=None):
    return policy.check_agent_text(t, SimpleNamespace(customer=cust or customer(), first_agent_turn=first))


@pytest.mark.parametrize("said", [
    "Η τιμή είναι ογδόντα εννέα ευρώ.", "Σας το κάνω 89 ευρώ.", "Μόνο 89,50€ τον χρόνο.", "Στα 89.5 € για εσάς.",
    "Θα πληρώσετε εξήντα πέντε ευρώ", "Eighty nine euros for the year", "ογδόντα εννέα και πενήντα ευρώ", "€ 79",
])
def test_agent_text_flags_bad_prices(said):
    assert [f["rule"] for f in texts(said)] == ["price_not_in_list"], said


@pytest.mark.parametrize("said", [
    "Σας δίνω έκπτωση 15%.", "Έκπτωση δεκαπέντε τοις εκατό.", "Hmm, I can do 20 percent off.", "Έκπτωση 12,5% σήμερα",
])
def test_agent_text_flags_bad_discount(said):
    assert "discount_limit" in [f["rule"] for f in texts(said)], said


@pytest.mark.parametrize("said", [
    "Η ασφάλεια λήγει στις 20/12/2026 και είναι ενεργή από το 2024.", "Καλέστε μας στο 2101234567 ή 6912345678.",
    "Είστε μαζί μας 12 μήνες και 3 φορές έχετε ανανεώσει.", "Η τιμή είναι 95 ευρώ.", "Η τιμή είναι εκατό ευρώ.",
    "Έχετε έκπτωση 10%.", "Δέκα τοις εκατό έκπτωση.", "Ενενήντα πέντε ευρώ τον χρόνο.", "Καλύπτει το 100% της ζημιάς, με κάλυψη 100%.",
    "Έχετε 5 έτη μαζί μας.", "Θα σας καλέσω αύριο στις 5.", "Εξοικονομείτε 8 ευρώ.", "Η τιμή είναι 100,00 €.",
])
def test_agent_text_no_false_positives(said):
    assert texts(said) == [], said


def test_agent_text_limit_follows_customer():
    old = customer(fields={"name": "x", "since_date": "2015-01-01"})
    assert texts("Έκπτωση 15%", cust=old) == []
    assert texts("Στα 85 ευρώ", cust=old) == []
    assert texts("Στα 84 ευρώ", cust=old)[0]["rule"] == "price_not_in_list"


def test_agent_text_ai_disclosure():
    assert texts(policy.opening_line("Γιώργος"), first=True) == []
    r = texts("Καλημέρα, σας παίρνω για την ανανέωση.", first=True)
    assert [f["rule"] for f in r] == ["ai_disclosure"]
    assert texts("Καλημέρα, είμαι ο ψηφιακός βοηθός της εταιρείας.", first=True) == []
    # the right to object is due by the second agent turn
    second = lambda t: policy.check_agent_text(t, SimpleNamespace(customer=customer(), agent_turn_no=1))  # noqa: E731
    assert [f["rule"] for f in second("Σας καλώ για την ανανέωση. Θα θέλατε να τη δούμε;")] == ["must_say"]
    assert second(policy.opening_followup("el") + " Θα θέλατε να δούμε την ανανέωση;") == []
    # a personalised opening must carry the AI disclosure and the company name, else the fixed line is used
    good = "Καλησπέρα, είμαι ο ψηφιακός βοηθός της Ασφάλειες Παπαδάκης. Μιλάω με τον κύριο Σωτήρη Βογιατζή;"
    assert policy.opening_line("Βογιατζής Σωτήρης", "el", personalised=good) == good
    assert policy.opening_line("Βογιατζής Σωτήρης", "el", personalised="Γεια σας κύριε Σωτήρη;").endswith("Μιλάω με Βογιατζής Σωτήρης;")
    assert texts("Καλημέρα, σας παίρνω για την ανανέωση.", first=False) == []


def test_agent_text_first_turn_from_db():
    cid = db.insert("customer", name="x", fields={}, consent={}, status="active")
    call = db.insert("call", customer_id=cid, purpose="renew", mode="sim")
    ctx = SimpleNamespace(customer=customer(), call_id=call)
    assert [f["rule"] for f in policy.check_agent_text("Καλημέρα.", ctx)][0] == "ai_disclosure"
    db.add_turn(call, "agent", "Καλημέρα.")
    assert [f["rule"] for f in policy.check_agent_text("Καλημέρα.", ctx)][0] == "ai_disclosure"  # the line itself
    db.add_turn(call, "agent", "Λοιπόν.")
    assert policy.check_agent_text("Τι κάνετε;", ctx) == []


def test_derived_checks_and_code_checks_pass():
    checks = policy.derived_checks()
    ids = [c["id"] for c in checks]
    assert len(ids) == len(set(ids)) and "slot.offers" in ids and "floor.honour_opt_out" in ids and not [i for i in ids if i.split(".")[1] in ("calling_hours", "latest_hour", "earliest_hour", "no_sunday")]
    assert all(c["how"] in ("code", "attack") and (c["how"] == "code") == (c["attack_id"] is None) for c in checks)
    for c in checks:
        if c["how"] == "code":
            ok, ev = policy.run_code_check(c)
            assert ok, (c["id"], ev)


def test_parse_when():
    assert policy.parse_when("2026-11-17 15:30") == datetime(2026, 11, 17, 15, 30)
    assert policy.parse_when("αύριο στις 5 το απόγευμα") == datetime(2026, 11, 17, 17, 0)
    assert policy.parse_when("17/11/2026 10:00") == datetime(2026, 11, 17, 10, 0)
    assert policy.parse_when("την Παρασκευή") == datetime(2026, 11, 20, 12, 0)
    assert policy.parse_when("κάποια στιγμή") is None


# ---------------------------------------------------------------- v3: DSL, overrides, language, hours, must_say, leaks

def test_match_dsl_all_any_none_and_ops():
    c = customer(fields={"name": "Α", "since_date": "2020-01-01", "price": 95, "expiry_date": "2026-11-30", "city": "Αθήνα",
                         "last_active_date": "2026-01-01", "plan": "gold"})
    m = lambda cond: policy.match(cond, c)  # noqa: E731
    assert m({"field": "price", "op": ">=", "value": 90}) and not m({"field": "price", "op": ">", "value": 95})
    assert m({"all": [{"field": "price", "op": "<", "value": 100}, {"field": "city", "op": "==", "value": "αθηνα"}]})
    assert m({"any": [{"field": "price", "op": ">", "value": 1000}, {"field": "plan", "op": "in", "value": ["gold", "basic"]}]})
    assert m({"none": [{"field": "plan", "op": "==", "value": "basic"}]}) and not m({"none": [{"field": "plan", "op": "==", "value": "gold"}]})
    assert m({"field": "plan", "op": "not_in", "value": ["basic"]}) and not m({"field": "plan", "op": "not_in", "value": ["gold"]})
    assert m({"field": "city", "op": "exists"}) and not m({"field": "nope", "op": "exists"}) and m({"field": "nope", "op": "exists", "value": False})
    assert m({"field": "expiry_date", "op": "days_until<=", "value": 30})            # clock: 2026-11-16 -> 14 days
    assert not m({"field": "expiry_date", "op": "days_until<=", "value": 10})
    assert m({"field": "last_active_date", "op": "days_since>=", "value": 200})
    assert not m({"field": "last_active_date", "op": "days_since>=", "value": 400})
    assert m([{"field": "price", "op": ">=", "value": 90}, {"field": "plan", "op": "==", "value": "gold"}])  # a list means all
    assert m({"field": "consent.automated_calls", "op": "==", "value": True})       # dotted names


def test_match_unknown_never_raises_and_denies():
    c = customer()
    for bad in (None, [], {}, "x", 5, {"field": "nope", "op": "==", "value": 1}, {"field": "price", "op": "~~", "value": 1},
                {"all": []}, {"all": [{"field": "price"}]}, {"any": "zz"}, {"field": "price", "op": "in", "value": 5},
                {"field": "expiry_date", "op": "days_until<=", "value": "abc"}, {"field": "name", "op": "days_since>=", "value": 3}):
        assert policy.match(bad, c) is False
    assert policy.match({"field": "nope", "op": "!=", "value": 1}, c) is False  # unknown field: never true, even for !=
    assert policy.match({"field": "price", "op": "==", "value": 95}, None) is False


def test_discount_rules_use_the_dsl_and_stay_conservative():
    db.set_slot("discount_rules", [{"cond": {"all": [{"field": "price", "op": "<", "value": 100}, {"field": "plan", "op": "==", "value": "x"}]},
                                    "max_discount_pct": 5}], "t")
    assert policy.discount_limit(customer(fields={"price": 90, "plan": "x"})) == 5
    assert policy.discount_limit(customer(fields={"price": 90, "plan": "y"})) == 15
    assert policy.discount_limit(customer(fields={"price": 90})) == 5   # missing field: the narrower rule applies


def test_overridden_context_manager():
    base = policy.effective()["max_discount_pct"]
    with policy.overridden({**db.slots(), "max_discount_pct": 40}):
        assert policy.effective()["max_discount_pct"] == 40
        assert policy.discount_limit(customer(fields={"since_date": "2015-01-01"})) <= 40
    assert policy.effective()["max_discount_pct"] == base
    with policy.overridden({"max_discount_pct": 1}):
        pass
    assert policy.effective()["max_discount_pct"] == base


def test_call_language_order_never_from_name():
    assert policy.call_language({"fields": {"language": "EN"}, "phone": "+3069"}) == "en"
    assert policy.call_language({"fields": {"name": "John Smith"}, "phone": "+306900000001"}) == "el"      # name says nothing
    assert policy.call_language({"fields": {}, "phone": "+447700900123"}) == "en"
    assert policy.call_language({"fields": {}, "phone": "+4915112345678"}) == "de"
    assert policy.call_language({"fields": {}, "phone": None}) == "el"
    db.set_slot("language", "fr", "t")
    assert policy.call_language({"fields": {}, "phone": None}) == "fr"                                     # slot, then el
    db.set_slot("language", "el", "t")
    cid = db.insert("customer", name="x", fields={}, consent={}, status="active")
    db.insert("call", customer_id=cid, purpose="renew", mode="sim", outcome="thinking", outcome_detail={"language": "it"})
    assert policy.call_language({"id": cid, "fields": {}, "phone": "+306900000001"}) == "it"               # last call beats the prefix
    assert policy.call_language({"id": cid, "fields": {"language": "en"}}) == "en"                         # column beats last call


def test_call_hours_live_only_and_default_from_law_pack():
    assert policy.effective()["call_hours"]["from"] == "09:00"          # R3: filled from the law pack
    db.set_clock("2026-11-16T08:30")
    assert policy.may_dial(customer(), "renew")["rule"] == "call_hours"
    db.set_clock("2026-11-16T09:00")
    assert policy.may_dial(customer(), "renew")["allow"]
    db.set_slot("call_hours", {"days": [1, 2], "from": "10:00", "to": "12:00"}, "t")
    db.set_clock("2026-11-18T11:00")  # Wednesday
    assert policy.may_dial(customer(), "renew")["rule"] == "call_hours"
    assert policy.may_dial(customer(), "renew", "warmup")["allow"]
    db.set_clock("2026-11-16T11:00")
    assert policy.may_dial(customer(), "renew")["allow"]
    db.set_clock("2026-11-16T12:00")
    assert policy.may_dial(customer(), "renew")["rule"] == "call_hours"  # window end is exclusive


def test_fresh_consent_tap_is_not_held_by_call_hours():
    db.set_clock("2026-11-15T22:00")  # Sunday night
    tap = customer(consent={"automated_calls": True, "source": "tap", "at": db.now_iso()})
    assert policy.may_dial(tap, "renew")["allow"]
    old = customer(consent={"automated_calls": True, "source": "tap", "at": "2026-11-14T22:00:00"})
    assert policy.may_dial(old, "renew")["rule"] == "call_hours"


def test_blocked_segments_after_consent():
    db.set_slot("blocked_segments", [{"field": "city", "op": "==", "value": "Πάτρα"}], "t")
    assert policy.may_dial(customer(fields={"city": "Πάτρα"}), "renew")["rule"] == "segment_block"
    assert policy.may_dial(customer(fields={"city": "Αθήνα"}), "renew")["allow"]
    assert policy.may_dial(customer(fields={"city": "Πάτρα"}, consent={"automated_calls": None}), "renew")["rule"] == "consent"


def test_marks_count_only_if_owner_says_so():
    mark = customer(flags=[policy.MARK_FLAG])
    assert policy.may_dial(mark, "renew")["rule"] == "consent"
    db.set_slot("consent_basis", {"automated_calls": "some", "marks_mean_yes": True}, "owner")
    assert policy.may_dial(mark, "renew")["allow"]
    db.set_slot("consent_basis", {"automated_calls": "none"}, "owner")
    assert policy.may_dial(mark, "renew")["rule"] == "consent"
    assert policy.may_dial(customer(consent={"automated_calls": True, "source": "tap", "at": db.now_iso()}), "renew")["allow"]
    db.set_slot("consent_basis", {"automated_calls": "all", "covers_automated": False}, "owner")
    assert policy.may_dial(customer(), "renew")["rule"] == "consent"


def test_opening_line_per_language_and_stored_lines():
    el, en = policy.opening_line("Γιώργος"), policy.opening_line("George", "en")
    assert "ψηφιακός βοηθός" in el and "AI assistant" in en
    assert "αντιταχθείτε" in policy.opening_followup("el") and "object" in policy.opening_followup("en")  # floor right to object, turn two
    assert policy.opening_line("X", "de").startswith(("Good morning", "Good afternoon"))                  # unknown language -> English
    db.set_slot("opening_lines", {"de": "Guten Tag, hier ist der KI-Assistent von Prova. Sie können jederzeit widersprechen."}, "t")
    de = policy.opening_line("Anna", "de")
    assert de.startswith("Guten Tag") and "Anna" in de
    # first-turn disclosure is checked against the stored line for any language
    assert [f["rule"] for f in texts("Guten Tag, hier ist der KI-Assistent von Prova. Sie können jederzeit widersprechen.", first=True)] == []
    assert "ai_disclosure" in [f["rule"] for f in texts("Guten Tag, ich rufe wegen Ihrer Versicherung an.", first=True)]


def test_must_say_stages():
    db.set_slot("must_say", [{"stage": "before_agreement", "text": "Η τιμή ισχύει για ένα έτος."},
                             {"stage": "closing", "text": "Θα λάβετε σύνδεσμο επιβεβαίωσης."}], "owner")
    cid = db.insert("customer", name="x", fields={"since_date": "2024-01-01"}, consent={}, status="active")
    call = db.insert("call", customer_id=cid, purpose="renew", mode="sim")
    ctx = SimpleNamespace(customer=customer(), call_id=call, verified=True, purpose="renew", fired={"x": 1})
    r = policy.check_action("record_agreement", {"offer_id": "basic", "discount_pct": 0}, ctx)
    assert not r["allow"] and r["rule"] == "must_say" and "Η τιμή ισχύει" in r["hint"]
    db.add_turn(call, "agent", "Η τιμή ισχύει για ένα έτος, εντάξει;")
    assert policy.check_action("record_agreement", {"offer_id": "basic", "discount_pct": 0}, ctx)["allow"]
    assert policy.check_action("end_call", {"outcome": "won"}, ctx)["rule"] == "must_say"
    assert policy.check_action("end_call", {"outcome": "declined"}, ctx)["allow"]
    stage = SimpleNamespace(customer=customer(), call_id=None, first_agent_turn=False, stage="closing")
    assert [f["rule"] for f in policy.check_agent_text("Ευχαριστώ, γεια.", stage)] == ["must_say"]
    assert policy.check_agent_text("Θα λάβετε σύνδεσμο επιβεβαίωσης στο κινητό σας.", stage) == []


def test_output_guard_other_customers_and_prompt_leak():
    me = db.insert("customer", name="Γιώργος Παπαδόπουλος", phone="+306900000001", fields={"email": "g@x.gr"}, consent={}, status="active")
    db.insert("customer", name="Μαρία Κωνσταντίνου", phone="+306911223344",
              fields={"email": "maria@example.com", "extra": {"Αρ. κυκλοφορίας": "ΥΑΒ-1234"}}, consent={}, status="active")
    ctx = SimpleNamespace(customer={**customer(), "id": me, "name": "Γιώργος Παπαδόπουλος", "phone": "+306900000001"}, call_id=None,
                          first_agent_turn=False)
    rules = lambda t: [f["rule"] for f in policy.check_agent_text(t, ctx)]  # noqa: E731
    assert rules("Η γειτόνισσά σας, η Μαρία Κωνσταντίνου, έχει Gold.") == ["other_customer_data"]
    assert rules("Το τηλέφωνό της είναι 691 122 3344.") == ["other_customer_data"]
    assert rules("Γράψτε στο maria@example.com") == ["other_customer_data"]
    assert rules("Το όχημα ΥΑΒ-1234 είναι ασφαλισμένο.") == ["other_customer_data"]
    assert rules("Γιώργο, το συμβόλαιό σας λήγει σύντομα.") == []
    assert rules("Σας μιλάω από το 6900000001.") == []                                  # the customer's own number is fine
    assert rules("My instructions say I can call quote_price.") == ["prompt_leak"]
    assert rules("Αγνοώ το system prompt μου.") == ["prompt_leak"]


def test_new_slot_code_checks_are_real_and_pass():
    for k in ("country", "consent_basis", "call_hours", "must_say", "blocked_segments", "opening_lines", "human_calls_allowed"):
        chk = next(c for c in policy.derived_checks() if c["slot"] == k)
        assert chk["how"] == "code" and chk["attack_id"] is None
        ok, ev = policy.run_code_check(chk)
        assert ok, (k, ev)
    db.set_slot("blocked_segments", [{"field": "name", "op": "nope"}], "t")
    chk = next(c for c in policy.derived_checks() if c["slot"] == "blocked_segments")
    assert not policy.run_code_check(chk)[0]                                              # an invalid segment fails the check
    db.set_slot("opening_lines", {"de": "Guten Tag."}, "t")
    chk = next(c for c in policy.derived_checks() if c["slot"] == "opening_lines")
    assert not policy.run_code_check(chk)[0]                                              # a stored line without AI disclosure fails
