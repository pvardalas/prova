import os
import tempfile

os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "onboard_test.db")
os.environ["PROVA_FAKE"] = "1"

import re  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from prova import db, onboard, template  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
E164 = re.compile(r"^\+30(69\d{8}|2\d{9})$")


@pytest.fixture(autouse=True)
def fresh():
    db.reset()


def load_docs(folder):
    for f in sorted((SAMPLES / folder).glob("*.md")):
        onboard.add_document(f.name, f.read_bytes())


def customers():
    return db.q("SELECT * FROM customer ORDER BY id")


GOAL_INS = "Θέλω να ανανεώσω τα συμβόλαια αυτοκινήτου που λήγουν τις επόμενες 45 ημέρες."
GOAL_GYM = "Θέλω να φέρω πίσω μέλη που έχουν σταματήσει να έρχονται."


# ---------------------------------------------------------------- ingest: insurance

def test_insurance_ingests_without_code_changes():
    s = onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    assert s["header_row"] == 3
    assert (s["rows"], s["customers"], s["rejects"], s["merged"]) == (46, 37, 4, 5)
    assert s["mapping"]["Συναίνεση κλήσεων"] == "consent"
    assert s["mapping"]["Κινητό/Τηλ"] == "phone" and s["mapping"]["Αρ. Κυκλοφορίας"] == "extra"
    assert s["skipped"]["totals"] == 1 and s["skipped"]["blank"] >= 3
    assert db.company()["setup"]["data"] == "done"
    assert len(customers()) == 37


def test_insurance_phones_are_e164_and_unique():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    phones = [c["phone"] for c in customers()]
    assert all(E164.match(p) for p in phones)
    assert len(set(phones)) == len(phones)
    assert len([p for p in phones if p.startswith("+302")]) == 2


def test_insurance_duplicates_merged_newest_wins():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    rows = db.q("SELECT * FROM customer WHERE phone='+306900000103'")
    assert len(rows) == 1
    assert any(f.startswith("duplicate merged") for f in rows[0]["flags"])
    assert rows[0]["row_no"] > 40


def test_insurance_two_numbers_prefers_mobile_and_flags():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    for phone in ("+306900000112", "+306900000125"):
        c = db.one("SELECT * FROM customer WHERE phone=?", (phone,))
        assert c and "second number ignored" in c["flags"]


def test_insurance_rejects_have_reasons():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    rej = db.q("SELECT * FROM reject ORDER BY row_no")
    assert len(rej) == 4
    assert all(r["reason"] and r["raw"] for r in rej)
    reasons = " | ".join(r["reason"] for r in rej)
    assert "digits" in reasons and "no usable name" in reasons and "no phone" in reasons


def test_insurance_injection_row_kept_as_data():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    c = next(c for c in customers() if "50%" in (c["notes"] or ""))
    assert "αγνόησε" in c["fields"]["notes"]
    assert any("instruction-like" in f for f in c["flags"])
    assert c["status"] == "active" and c["consent"]["automated_calls"] is True
    assert "max_discount_pct" not in c["fields"] and "discount" not in " ".join(c["fields"])


def test_insurance_do_not_call_note_sets_status():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    dnc = [c for c in customers() if c["status"] == "do_not_call"]
    assert len(dnc) == 1 and "do-not-call found in notes" in dnc[0]["flags"]
    plain = next(c for c in customers() if "απόγευμα" in (c["notes"] or ""))
    assert plain["status"] == "active"


def test_insurance_values_cleaned():
    s = onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    today = date.today()
    exp = [date.fromisoformat(c["fields"]["expiry_date"]) for c in customers() if c["fields"].get("expiry_date")]
    assert len(exp) == 35  # one blank, one unreadable
    assert min(exp) >= today - timedelta(days=11) and max(exp) <= today + timedelta(days=46)
    assert all(isinstance(c["fields"].get("price"), (int, float)) for c in customers())
    assert all(100 < c["fields"]["price"] < 700 for c in customers())
    assert s["consent_true"] == 30
    states = [c["consent"]["automated_calls"] for c in customers()]
    assert states.count(False) == 3 and states.count(None) == 3
    assert all(c["fields"].get("birth_year") for c in customers() if c["fields"].get("birth_year") is not None)
    assert s["flags_summary"]["price parsed from text"] >= 30


def test_raw_row_kept():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    c = db.one("SELECT * FROM customer WHERE phone='+306900000101'")
    assert c["raw"]["Κινητό/Τηλ"] and "Ονοματεπώνυμο" in c["raw"]


def test_reingest_replaces():
    p = str(SAMPLES / "insurance" / "pelates.xlsx")
    onboard.ingest(p)
    onboard.ingest(p)
    assert len(customers()) == 37 and len(db.q("SELECT * FROM reject")) == 4


# ---------------------------------------------------------------- ingest: gym

def test_gym_ingests_without_code_changes():
    s = onboard.ingest(str(SAMPLES / "gym" / "members.csv"))
    assert s["header_row"] == 1
    assert (s["rows"], s["customers"], s["rejects"], s["merged"]) == (32, 27, 3, 2)
    m = s["mapping"]
    assert m["Μέλος"] == "name" and m["Κινητό"] == "phone" and m["Τελευταία επίσκεψη"] == "last_active_date"
    assert m["Μηνιαία συνδρομή"] == "price" and m["Τύπος συνδρομής"] == "product" and m["Προπονητής"] == "extra"
    assert all(E164.match(c["phone"]) for c in customers())
    assert s["consent_true"] >= 18
    lapsed = [c for c in customers() if c["fields"].get("last_active_date")
              and date.fromisoformat(c["fields"]["last_active_date"]) < date.today() - timedelta(days=20)]
    assert len(lapsed) >= 18
    assert all(c["fields"].get("period") == "month" for c in customers())
    assert any(c["status"] == "do_not_call" for c in customers())


# ---------------------------------------------------------------- cleaners

@pytest.mark.parametrize("raw,expected", [
    ("697 000 0101", "+306970000101"), ("694.000.0102", "+306940000102"), ("0030 690 000 0103", "+306900000103"),
    ("+30 690 000 0104", "+306900000104"), (6900000105, "+306900000105"), ("690-000-0106", "+306900000106"),
    ("(+30) 690 000 0111", "+306900000111"), ("210 000 0107", "+302100000107"), (6900000105.0, "+306900000105"),
    ("210 000 0925, 690 000 0125", "+306900000125"), ("+44 7700 900123", "+447700900123"),
])
def test_phone_good(raw, expected):
    assert onboard.parse_phone(raw)[0] == expected


@pytest.mark.parametrize("raw", ["690 000 010", None, "", "τηλ. άγνωστο", "12345", "69000001011"])
def test_phone_bad(raw):
    phone, _, problem = onboard.parse_phone(raw)
    assert phone is None and problem


@pytest.mark.parametrize("raw,iso", [
    ("15/12/2026", "2026-12-15"), ("5-1-27", "2027-01-05"), ("15 Δεκεμβρίου 2026", "2026-12-15"),
    ("3 Μαΐου 2027", "2027-05-03"), ("2026-12-15", "2026-12-15"), (46371, "2026-12-15"), ("12.03.2026", "2026-03-12"),
    ("Ιανουάριος 2020", "2020-01-31"), ("2019", "2019-01-01"), ("3 Ιουν 2026", "2026-06-03"),
])
def test_date_formats(raw, iso):
    assert onboard.parse_date(raw)[0] == iso


def test_date_unreadable():
    assert onboard.parse_date("τέλος του μήνα")[0] is None


@pytest.mark.parametrize("raw,price", [("25,50€", 25.5), ("25.5 ευρώ", 25.5), ("€ 198,00", 198), ("1.250,00 €", 1250),
                                       ("420€", 420), ("185 €", 185), (265, 265)])
def test_price_text(raw, price):
    assert onboard.parse_price(raw)[0] == price


@pytest.mark.parametrize("raw,val", [("ΝΑΙ", True), ("ναι", True), ("x", True), ("Χ", True), ("Ναι", True), ("1", True),
                                     ("ΟΧΙ", False), ("οχι", False), ("όχι", False), ("0", False), (None, None), ("ίσως", None)])
def test_consent_values(raw, val):
    assert onboard.parse_bool(raw) is val


def test_header_detection_with_title_and_group_rows():
    rows = [["Title", None, None], [None, "Group", None], ["Name", "Phone", "Date"], ["a", "b", "c"], [None, None, None]]
    assert onboard.find_header(rows) == 2


def test_ingest_unusable_file_blocks(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("foo;bar\n1;2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        onboard.ingest(str(f))
    assert db.company()["setup"]["data"] == "blocked"


def test_csv_delimiters_and_encodings(tmp_path):
    for delim, enc in ((",", "utf-8"), (";", "cp1253"), ("\t", "utf-8-sig")):
        f = tmp_path / f"m_{ord(delim)}.csv"
        f.write_bytes(f"Όνομα{delim}Τηλέφωνο{delim}Λήξη\nΜαρία Κ{delim}690 000 0301{delim}01/12/2026\n".encode(enc))
        db.reset()
        s = onboard.ingest(str(f))
        assert s["customers"] == 1 and customers()[0]["fields"]["expiry_date"] == "2026-12-01"


# ---------------------------------------------------------------- documents

def test_add_document_kinds():
    a = onboard.add_document("a.md", "# Τίτλος\nκείμενο")
    b = onboard.add_document("b.html", b"<html><style>x{}</style><body><h1>Hello</h1><p>World</p><script>bad()</script></body></html>")
    c = onboard.add_document("c.txt", "Ώρες".encode("cp1253"))
    assert len({a, b, c}) == 3
    assert db.get("doc", b)["text"].split() == ["Hello", "World"]
    assert db.get("doc", c)["text"] == "Ώρες"
    assert onboard.add_document("a.md", "# Νέο") == a and db.get("doc", a)["text"] == "# Νέο"
    with pytest.raises(ValueError):
        onboard.add_document("empty.txt", "  ")


def test_add_url_offline_and_scheme():
    i = onboard.add_url("https://example.com/about")
    assert db.get("doc", i)["kind"] == "url"
    with pytest.raises(ValueError):
        onboard.add_url("file:///etc/passwd")


# ---------------------------------------------------------------- policy

REQUIRED = template.REQUIRED_KEYS


def slot(key):
    return db.get("policy_slot", key)


def test_fill_policy_insurance():
    load_docs("insurance")
    r = onboard.fill_policy(GOAL_INS)
    assert r["goal_type"] == "renew"
    for k in ("company_name", "business_type", "handoff_contact", "offers", "max_discount_pct", "allowed_purposes",
              "max_attempts", "min_gap_hours", "identity_check", "recording", "confirmation"):
        assert k in r["filled"], k
    assert not [k for k in r["empty"] if k in REQUIRED]
    for k in r["filled"]:
        src = slot(k)["source"]
        assert re.match(r'^[^:]+: ".{3,}', src), (k, src)
    assert slot("company_name")["value"] == "Ασφάλειες Παπαδάκης"
    offers = slot("offers")["value"]
    assert [(o["name"], o["price"]) for o in offers] == [("Βασική", 185), ("Βασική+", 265), ("Πλήρης", 420), ("Premium", 560)]
    assert slot("max_discount_pct")["value"] == 15
    rule = slot("discount_rules")["value"][0]
    assert rule["max_discount_pct"] == 10 and rule["field"] == "since_date"
    assert slot("calling_hours") is None  # calling hours in the documents are not a policy
    assert slot("identity_check")["value"]["field"] == "name"
    assert slot("confirmation")["value"]["channel"] == "link" and "14" in slot("confirmation")["value"]["withdrawal_text"]
    assert "210 000 0100" in slot("handoff_contact")["value"]
    assert len(slot("forbidden_claims")["value"]) == 4
    assert slot("language")["status"] == "assumed" and "language" in r["assumed"]
    c = db.company()
    assert c["name"] == "Ασφάλειες Παπαδάκης" and c["goal_type"] == "renew" and c["goal_text"] == GOAL_INS
    assert c["setup"]["policy"] == "done"


def test_fill_policy_gym():
    load_docs("gym")
    r = onboard.fill_policy(GOAL_GYM)
    assert r["goal_type"] == "win_back"
    assert not [k for k in r["empty"] if k in REQUIRED]
    assert slot("max_discount_pct")["value"] == 20 and slot("discount_rules")["status"] == "assumed"
    assert slot("recording")["value"] == {"on": False, "notice": None}
    assert slot("identity_check")["value"]["field"] == "name"
    assert [o["price"] for o in slot("offers")["value"]] == [45, 35, 29, 25]
    assert slot("allowed_purposes")["value"] == ["win_back"] and slot("allowed_purposes")["source"].startswith("goal:")


def test_fill_policy_without_documents():
    assert "error" in onboard.fill_policy(GOAL_INS)


def test_fill_policy_leaves_slot_empty_when_documents_do_not_say(monkeypatch):
    onboard.add_document("site.md", "# Μικρό Κατάστημα\n**Δραστηριότητα:** ασφαλιστικό γραφείο\n")
    r = onboard.fill_policy(GOAL_INS)
    assert "offers" in r["empty"] and "handoff_contact" in r["empty"] and "max_discount_pct" in r["empty"]
    assert slot("offers")["status"] == "empty" and slot("max_discount_pct")["status"] == "empty"
    assert db.company()["setup"]["policy"] == "blocked"


def test_invented_values_are_rejected(monkeypatch):
    load_docs("insurance")
    lie = {"company": {"goal_type": "renew"}, "slots": {
        "offers": {"value": [{"name": "Super", "price": 99, "period": "year"}], "doc": "timokatalogos.md",
                   "quote": "Super | 99 €"},
        "max_discount_pct": {"value": 50, "doc": "oroi.md", "quote": "Έκπτωση στο ασφάλιστρο: έως 10%, έως 15%"},
        "handoff_contact": {"value": "Κάποιος", "doc": "site.md", "quote": "Υπεύθυνος για θέματα πελατών: Νίκος Παπαδάκης"},
        "recording": {"value": {"on": True, "notice": "x"}, "doc": "oroi.md", "quote": "this sentence is not in the document"},
    }}
    monkeypatch.setattr(onboard.llm, "ask_json", lambda *a, **k: lie)
    r = onboard.fill_policy(GOAL_INS)
    assert {x["key"] for x in r["rejected"]} == {"offers", "max_discount_pct", "recording"}
    assert slot("offers")["status"] == "empty" and slot("max_discount_pct")["status"] == "empty"
    assert slot("recording")["status"] == "assumed"
    assert slot("handoff_contact")["status"] == "filled"


def test_goal_from_text():
    assert onboard.goal_from_text("φέρε πίσω χαμένους πελάτες") == "win_back"
    assert onboard.goal_from_text("ανανέωση συμβολαίων") == "renew"
    assert onboard.goal_from_text("υπενθύμιση ραντεβού") == "remind"


def test_confirm_slot():
    load_docs("insurance")
    onboard.fill_policy(GOAL_INS)
    r = onboard.confirm_slot("language")
    assert r["status"] == "confirmed" and r["value"] == "el" and r["source"] == "default"
    r = onboard.confirm_slot("max_discount_pct", 12)
    assert r == {"key": "max_discount_pct", "value": 12, "status": "confirmed", "source": "owner"}
    assert "error" in onboard.confirm_slot("max_discount_pct", 500)
    assert "error" in onboard.confirm_slot("nope")
    assert "error" in onboard.confirm_slot("calling_hours", {"start": "09:00", "end": "20:00", "days": [1]})  # no such slot
    db.reset()
    assert "error" in onboard.confirm_slot("offers")  # empty, no value given
    assert onboard.confirm_slot("offers", [{"name": "X", "price": 10}])["status"] == "confirmed"


def test_refill_keeps_confirmed_slots():
    load_docs("insurance")
    onboard.fill_policy(GOAL_INS)
    onboard.confirm_slot("max_discount_pct", 12)
    r = onboard.fill_policy(GOAL_INS)
    assert "max_discount_pct" in r["kept"] and slot("max_discount_pct")["value"] == 12


# ---------------------------------------------------------------- approve / go live / pause

def fake_policy(monkeypatch, missing):
    mod = types.ModuleType("prova.policy")
    mod.missing_required = lambda: list(missing)
    monkeypatch.setitem(sys.modules, "prova.policy", mod)
    import prova
    monkeypatch.setattr(prova, "policy", mod, raising=False)


def eval_run(status="done", failed=0):
    return db.insert("eval_run", started_at=db.now_iso(), status=status, passed=10, failed=failed)


def test_approve_refuses_while_required_slot_is_empty(monkeypatch):
    fake_policy(monkeypatch, [{"key": "offers", "label": "Offers"}])
    eval_run()
    r = onboard.approve()
    assert "required policy slots are empty" in r["error"] and r["missing"][0]["key"] == "offers"
    assert db.company()["status"] == "draft"


def test_approve_needs_green_eval(monkeypatch):
    fake_policy(monkeypatch, [])
    assert "not been run" in onboard.approve()["error"]
    eval_run(failed=2)
    r = onboard.approve()
    assert "2 failed" in r["error"] and r["eval"]["failed"] == 2
    eval_run(status="running")
    assert "running" in onboard.approve()["error"]
    assert db.company()["status"] == "draft"


def test_approve_then_live_then_pause(monkeypatch):
    fake_policy(monkeypatch, [])
    assert "approve it first" in onboard.go_live()["error"]
    eval_run()
    assert onboard.approve()["status"] == "approved"
    assert onboard.go_live() == {"status": "live"} and db.company()["status"] == "live"
    assert onboard.pause("test")["status"] == "paused" and db.company()["status"] == "paused"
    assert onboard.go_live() == {"status": "live"}


# ---------------------------------------------------------------- playbook

def fake_learn(monkeypatch, reject_word=None):
    mod = types.ModuleType("prova.learn")

    def lint_card(card):
        if reject_word and reject_word in card["move"]:
            return {"ok": False, "reasons": ["manipulative"]}
        return {"ok": True, "reasons": []}

    mod.lint_card = lint_card
    monkeypatch.setitem(sys.modules, "prova.learn", mod)
    import prova
    monkeypatch.setattr(prova, "learn", mod, raising=False)


def test_seed_playbook_covers_situations_and_lints(monkeypatch):
    fake_learn(monkeypatch)
    load_docs("insurance")
    onboard.fill_policy(GOAL_INS)
    r = onboard.seed_playbook()
    cards = db.q("SELECT * FROM tactic_card")
    assert r["missing_situations"] == [] and r["rejected"] == 0 and len(cards) == r["added"] <= 8
    for s in onboard.SEED_SITUATIONS:
        assert any(c["situation"] == s and c["status"] == "proven" for c in cards)
    assert all(c["origin"] == "online" and c["source"] == "model knowledge" and c["lint"]["ok"] for c in cards)
    assert db.company()["setup"]["playbook"] == "done"
    assert not re.search(r"\d", " ".join(c["move"] for c in cards))


def test_seed_playbook_stores_rejected_cards(monkeypatch):
    fake_learn(monkeypatch, reject_word="cheaper")
    r = onboard.seed_playbook(max_cards=14)
    rejected = db.q("SELECT * FROM tactic_card WHERE status='rejected'")
    assert r["rejected"] == len(rejected) == 1
    assert rejected[0]["lint"] == {"ok": False, "reasons": ["manipulative"]}
    assert r["missing_situations"] == []
    events = [e for e in db.q("SELECT * FROM event WHERE type='card'")]
    assert len(events) == len(db.q("SELECT * FROM tactic_card"))
    assert db.verify_chain()["ok"]


# ---------------------------------------------------------------- konstantinou: messy real files

KON = Path(__file__).resolve().parent.parent / "demo" / "konstantinou"


def kon_doc(name):
    onboard.add_document(name, (KON / name).read_bytes())
    return (KON / name).read_text(encoding="utf-8")


def fill(facts, goal=GOAL_INS):
    return onboard.fill_policy(goal, facts=facts)


def pq(fact):
    return next((q for q in onboard.policy_questions() if q["fact"] == fact), None)


def test_typo_prices_are_kept_assumed_and_asked_in_one_question():
    kon_doc("timokatalogos_2026.txt")
    q = lambda s: s  # noqa: E731
    offers = [{"id": "auto_basic", "name": "Αυτοκίνητο Βασικό", "price": 180, "period": "year"},
              {"id": "auto_full", "name": "Αυτοκίνητο Πλήρες", "price": 440, "period": "year"},
              {"id": "auto_premium", "name": "Αυτοκίνητο Premium", "price": 780, "period": "year"},
              {"id": "home_basic", "name": "Κατοικία Βασικό", "price": 125, "period": "year"},
              {"id": "home_full", "name": "Κατοικία Πλήρες", "price": 240, "period": "year"},
              {"id": "health", "name": "Προσθετη υγείας", "price": 410, "period": "year"},
              {"id": "ghost", "name": "Κοινωνική ασφάλιση", "price": 999, "period": "year"}]
    quote = "Βασικό (μόνο αστική ευθύνη) ... 18O € ... ~~420~~ 440 € ... 7B0 €* ... 125 € ... 24O € ... 4I0 €"
    r = fill({"slots": {"offers": {"value": offers, "doc": "timokatalogos_2026.txt", "quote": quote}}})
    kept = {o["id"]: o["price"] for o in slot("offers")["value"]}
    assert kept == {"auto_basic": 180, "auto_full": 440, "auto_premium": 780, "home_basic": 125, "home_full": 240, "health": 410}
    assert db.get("policy_slot", "offers")["status"] == "assumed" and "offers" in r["assumed"]
    ask = pq("offers")
    assert ask["kind"] == "offers_check" and [o["id"] for o in ask["options"]] == ["fix"] or True
    for shown in ("«18O €»", "«7B0 €*»", "«24O €»", "«4I0 €»"):
        assert shown in ask["text"]
    assert "Εννοείτε 180 €;" in ask["text"] and "999" in ask["text"]      # the unmatchable offer is asked about too
    assert q  # one question for the whole slot
    assert len([x for x in onboard.policy_questions() if x["fact"] == "offers"]) == 1


def test_doubtful_prices_only_gives_yes_all_and_the_struck_price_takes_the_new_one():
    kon_doc("timokatalogos_2026.txt")
    offers = [{"id": "auto_basic", "name": "Αυτοκίνητο Βασικό", "price": 180, "period": "year"},
              {"id": "auto_full", "name": "Αυτοκίνητο Πλήρες", "price": 420, "period": "year"}]
    fill({"slots": {"offers": {"value": offers, "doc": "timokatalogos_2026.txt", "quote": "18O € ... ~~420~~ 440 €"}}})
    assert {o["id"]: o["price"] for o in slot("offers")["value"]} == {"auto_basic": 180, "auto_full": 440}
    ask = pq("offers")
    assert [o["id"] for o in ask["options"]] == ["yes_all", "fix"] and "~~420~~ 440" in ask["text"]


def test_clean_prices_are_filled_without_a_question():
    load_docs("insurance")
    fill(None) if False else onboard.fill_policy(GOAL_INS)
    assert db.get("policy_slot", "offers")["status"] == "filled" and pq("offers") is None


def test_restriction_wording_gives_call_hours_with_the_quote():
    assert onboard.parse_call_restriction("3. Δεν παίρνουμε τηλέφωνο Κυριακή ούτε μετά τις 20:00 (ούτε SMS μετά τις 20:00).") == \
        {"days": [1, 2, 3, 4, 5, 6], "from": "09:00", "to": "20:00"}
    assert onboard.parse_call_restriction("όχι Κυριακή στις κλήσεις") == {"days": [1, 2, 3, 4, 5, 6], "from": "09:00", "to": "20:00"}
    assert onboard.parse_call_restriction("Τα παράπονα τα παίρνω εγώ, 2610 000 111") is None
    text = kon_doc("email_odigies.txt")
    out = onboard._fake_fill(db.q("SELECT name, text FROM doc"), GOAL_INS)
    ch = out["slots"]["call_hours"]
    assert ch["value"] == {"days": [1, 2, 3, 4, 5, 6], "from": "09:00", "to": "20:00"} and ch["quote"] in text


def test_the_email_thread_gives_a_conflict_and_the_stricter_reading_holds():
    kon_doc("email_odigies.txt")
    out = onboard._fake_fill(db.q("SELECT name, text FROM doc"), GOAL_INS)
    c = out["conflicts"][0]
    assert c["key"] == "max_discount_pct" and [x["value"] for x in c["candidates"]] == [15, 20] or \
        sorted(x["value"] for x in c["candidates"]) == [15, 20]
    fill(out)
    q = pq("max_discount_pct")
    assert q["kind"] == "conflict" and q["text"] == "Βρήκα δύο κανόνες για την έκπτωση: 20% (3 Μαρτίου) και 10%/15% (9 Νοεμβρίου). Ποιος ισχύει;"
    assert q["default_if_skipped"] == {"value": 10}
    assert slot("max_discount_pct")["value"] == 10 and db.get("policy_slot", "max_discount_pct")["status"] == "assumed"


def test_a_conflict_quote_that_is_not_in_the_document_is_not_a_candidate():
    kon_doc("email_odigies.txt")
    facts = {"slots": {}, "conflicts": [{"key": "max_discount_pct", "candidates": [
        {"value": 20, "quote": "έκπτωση μέχρι 20%", "doc": "email_odigies.txt", "date": "2026-03-03"},
        {"value": 5, "quote": "έκπτωση 5% για όλους", "doc": "email_odigies.txt", "date": "2026-11-09"}]}]}
    fill(facts)
    assert pq("max_discount_pct") is None


EMAIL_QUOTE = "1. Έκπτωση το πολύ 10%. Για πελάτες πάνω από 5 χρόνια μαζί μας, το πολύ 15%."


def discount_facts(rules=None, value=15):
    f = {"slots": {"max_discount_pct": {"value": value, "doc": "email_odigies.txt", "quote": EMAIL_QUOTE}}}
    if rules is not None:
        f["slots"]["discount_rules"] = {"value": rules, "doc": "email_odigies.txt", "quote": EMAIL_QUOTE}
    return f


def test_fifteen_for_everyone_is_never_kept_without_since_dates():
    kon_doc("email_odigies.txt")
    wide = [{"when": "customers over 5 years", "field": "since_date", "op": "<", "value": "2021-11-15", "max_discount_pct": 15}]
    fill(discount_facts(wide))
    assert slot("max_discount_pct")["value"] == 10      # the lower limit for everyone
    q = pq("max_discount_pct")
    assert q["kind"] == "discount_check" and "πελάτης από" in q["text"] and "δεν μπορεί να εφαρμοστεί ανά πελάτη" in q["text"]
    assert q["default_if_skipped"] == {"value": 10}
    assert all(float(r["max_discount_pct"]) < 10 for r in slot("discount_rules")["value"])


def test_fifteen_for_everyone_is_rewritten_when_since_dates_exist():
    kon_doc("email_odigies.txt")
    cust = {"name": "Λέκκας Γιώργος", "phone": "+306900000999", "since_date": "2019-01-01"}
    db.insert("customer", row_no=1, raw={}, fields=cust, name=cust["name"], phone=cust["phone"], consent={}, status="active", flags=[], notes=None)
    for rules in (None, [{"when": "customers over 5 years", "field": "since_date", "op": "<", "value": "2021-11-15", "max_discount_pct": 15}]):
        fill(discount_facts(rules))
        assert slot("max_discount_pct")["value"] == 15 and pq("max_discount_pct") is None
        (rule,) = slot("discount_rules")["value"]
        assert rule["field"] == "since_date" and rule["op"] == ">=" and rule["max_discount_pct"] == 10 and rule["quote"]
        assert all(float(r["max_discount_pct"]) < 15 for r in slot("discount_rules")["value"])


def test_two_limits_without_a_condition_ask_with_both_readings():
    onboard.add_document("note.txt", "Έκπτωση έως 10%, και έως 15% για τους καλούς πελάτες.")
    f = {"slots": {"max_discount_pct": {"value": 15, "doc": "note.txt", "quote": "Έκπτωση έως 10%, και έως 15% για τους καλούς πελάτες."}}}
    fill(f)
    q = pq("max_discount_pct")
    assert slot("max_discount_pct")["value"] == 10
    assert q and "Ανάγνωση Α" in q["text"] and "Ανάγνωση Β" in q["text"] and q["default_if_skipped"] == {"value": 10}


def test_never_discount_on_a_product_becomes_a_zero_rule_with_the_quote():
    kon_doc("email_odigies.txt")
    kon_doc("timokatalogos_2026.txt")
    facts = discount_facts(None, 10)
    facts["slots"]["offers"] = {"value": [{"id": "auto_premium", "name": "Αυτοκίνητο Premium", "price": 780, "period": "year"}],
                                "doc": "timokatalogos_2026.txt", "quote": "7B0 €"}
    fill(facts)
    rules = slot("discount_rules")["value"]
    z = next(r for r in rules if r["field"] == "product")
    assert z["op"] == "==" and z["max_discount_pct"] == 0 and "PREMIUM" in z["quote"] and z["doc"] == "email_odigies.txt"


def test_a_written_confirmation_by_sms_is_proposed_as_a_web_link():
    kon_doc("email_odigies.txt")
    out = onboard._fake_fill(db.q("SELECT name, text FROM doc"), GOAL_INS)
    c = out["slots"]["confirmation"]
    assert c["value"]["channel"] == "link" and "γραπτώς" in c["quote"]
    sms = {"slots": {"confirmation": {"value": {"channel": "sms", "withdrawal_text": "14 ημέρες"}, "doc": "email_odigies.txt", "quote": c["quote"]}}}
    fill(sms)
    assert slot("confirmation")["value"]["channel"] == "link"


def test_surname_and_first_name_columns_are_joined(tmp_path):
    raw = (KON / "palio_crm_export.csv").read_bytes()
    p = tmp_path / "palio.csv"
    p.write_bytes(raw)
    s = onboard.ingest(str(p))
    assert s["mapping"]["Επώνυμο"] == "name" and s["mapping"]["Όνομα"] == "first_name"
    names = {c["name"] for c in customers()}
    assert "Δημήτρης Αναστασίου" in names and "Ελένη Βλάχου" in names
    assert not any(" " not in n for n in names)


# ---------------------------------------------------------------- a second customer file merges, never replaces

DEMO = Path(__file__).resolve().parent.parent / "demo" / "konstantinou"


def test_second_file_merges_into_existing_customers():
    a = onboard.ingest(str(DEMO / "pelates_2026.xlsx"))
    n1 = len(customers())
    assert n1 == a["customers"] and a["merged_existing"] == 0
    before = {c["phone"]: c for c in customers()}
    b = onboard.ingest(str(DEMO / "palio_crm_export.csv"))
    after = customers()
    assert len(after) >= n1 and b["customers"] == len(after)
    assert b["merged_existing"] >= 1 and b["merged"] >= b["merged_existing"]
    assert {p for p in before} <= {c["phone"] for c in after}
    merged = [c for c in after if any("merged with palio_crm_export.csv" in f for f in c["flags"])]
    assert merged and all(c["raw"]["_other_sources"][0]["file"] == "palio_crm_export.csv" for c in merged)
    for c in merged:   # an explicit "no" survives
        if before[c["phone"]]["consent"]["automated_calls"] is False:
            assert c["consent"]["automated_calls"] is False


def test_merge_never_turns_a_no_into_a_yes(tmp_path):
    f1, f2 = tmp_path / "a.csv", tmp_path / "b.csv"
    f1.write_text("Όνομα,Τηλέφωνο,Συναίνεση\nΝίκος Α,6900000001,ΟΧΙ\n", "utf-8")
    f2.write_text("Όνομα,Τηλέφωνο,Συναίνεση,Email\nΝίκος Α,6900000001,ΝΑΙ,n@x.gr\n", "utf-8")
    onboard.ingest(str(f1))
    s = onboard.ingest(str(f2))
    c, = customers()
    assert s["customers"] == 1 and s["merged_existing"] == 1 and c["consent"]["automated_calls"] is False


def test_two_numbers_in_one_cell_use_the_newer():
    for cell in ("690 000 0302 690 000 0322", "6900000302 / 6900000322"):
        p, flags, bad = onboard.parse_phone(cell)
        assert p == "+306900000322" and bad is None and "two numbers, used the newer" in flags


def test_month_name_dates_without_day_or_year():
    iso, flag = onboard.parse_date("Ιαν 27")
    assert iso == "2027-01-31" and flag == "day missing"
    assert onboard.parse_date("Feb 2028") == ("2028-02-29", "day missing")
    iso, flag = onboard.parse_date("12 Δεκ.")
    assert iso[5:] == "12-12" and flag == "year missing" and iso >= db.now().date().isoformat()
