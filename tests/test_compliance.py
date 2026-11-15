import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "compliance.db")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402

from prova import compliance, db, law, policy  # noqa: E402

OFFERS = [{"id": "basic", "name": "Βασικό", "price": 100, "period": "year"}]


def add(name, consent, flags=None, status="active", phone="+3069000000", price=100, **fields):
    n = db.one("SELECT COUNT(*) n FROM customer")["n"]
    f = {"name": name, "phone": f"{phone}{n:02d}" if phone else None, "price": price, "expiry_date": "2026-12-01", "birth_year": 1970, **fields}
    return db.insert("customer", row_no=n + 1, fields=f, name=name, phone=f["phone"], consent=consent, status=status, flags=flags or [])


@pytest.fixture(autouse=True)
def seed():
    db.reset()
    db.update("company", 1, name="Παπαδάκης", goal_type="renew", status="live", budget_eur=5.0)
    for k, v in {"company_name": "Ασφάλειες Παπαδάκης", "business_type": "insurance broker", "language": "el", "country": "GR",
                 "offers": OFFERS, "max_discount_pct": 10, "allowed_purposes": ["renew"],
                 "identity_check": {"field": "birth_year", "question": "Έτος γέννησης;"}, "handoff_contact": "Νίκος 210"}.items():
        db.set_slot(k, v, "test", "confirmed")
    db.set_clock("2026-11-16T11:00")


def people():
    return {"yes": add("Yes One", {"automated_calls": True, "source": "Consent: 'ναι'"}),
            "mark": add("Mark One", {"automated_calls": True, "source": "Consent: 'x'"}, flags=[policy.MARK_FLAG], price=200),
            "blank": add("Blank One", {"automated_calls": None, "source": "Consent: None"}, price=300),
            "no": add("No One", {"automated_calls": False, "source": "Consent: 'όχι'"}),
            "dnc": add("Dnc One", {"automated_calls": True}, status="do_not_call"),
            "optout": add("Reg One", {"automated_calls": None}, opt_out_register=True),
            "tap": add("Tap One", {"automated_calls": True, "source": "tap", "at": "2026-11-16T10:00:00"}),
            "won": add("Won One", {"automated_calls": True}, status="won"),
            "nocontact": add("Nobody", {"automated_calls": None}, phone="")}


def lane_of(i):
    return compliance.lane(db.get("customer", i))


def test_each_customer_has_exactly_one_lane_and_reason():
    ids = people()
    got = {k: lane_of(i) for k, i in ids.items()}
    assert got["yes"]["lane"] == "ai_call" and got["yes"]["next"] == "call"
    assert got["tap"]["lane"] == "ai_call" and got["tap"]["rule"] == "consent_tap"
    assert got["mark"]["lane"] == "consent_request" and "mark" in got["mark"]["reason"]
    assert got["blank"]["lane"] == "consent_request" and got["blank"]["next"] == "send_request"
    assert got["no"]["lane"] == "hold" and got["no"]["rule"] == "consent_refused"
    assert got["dnc"]["lane"] == "hold" and got["optout"]["lane"] == "hold" and got["optout"]["rule"] == "opt_out_register"
    assert got["won"]["lane"] == "hold"
    assert got["nocontact"]["lane"] == "hold" and got["nocontact"]["rule"] == "no_lawful_channel"
    for r in got.values():
        assert r["lane"] in compliance.LANES and set(r) == {"lane", "rule", "reason", "next"}
    assert compliance.lane({}) ["lane"] == "hold"


def test_marks_count_only_when_owner_says_marks_mean_yes():
    i = add("M", {"automated_calls": True, "source": "x"}, flags=[policy.MARK_FLAG])
    assert lane_of(i)["lane"] == "consent_request"
    db.set_slot("consent_basis", {"automated_calls": "some", "marks_mean_yes": True}, "owner", "confirmed")
    assert lane_of(i)["lane"] == "ai_call"
    db.set_slot("consent_basis", {"automated_calls": "none"}, "owner", "confirmed")      # the owner says nobody consented
    assert lane_of(i)["lane"] == "consent_request"
    j = add("T", {"automated_calls": True, "source": "tap", "at": db.now_iso()})
    assert lane_of(j)["lane"] == "ai_call"                                                # a tap always counts


def test_may_dial_allows_only_ai_call_customers():
    ids = people()
    for k, i in ids.items():
        c = db.get("customer", i)
        allowed = policy.may_dial(c, "renew", "sim")["allow"]
        assert allowed == (compliance.lane(c)["lane"] == "ai_call"), k


def test_blocked_segments_hold_and_human_lane_off_for_gr():
    i = add("Seg", {"automated_calls": True}, city="Πάτρα")
    db.set_slot("blocked_segments", [{"field": "city", "op": "==", "value": "Πάτρα"}], "owner")
    assert lane_of(i)["rule"] == "segment_block"
    db.set_slot("human_calls_allowed", True, "owner", "confirmed")
    j = add("NoPhone", {"automated_calls": None}, phone="")
    assert lane_of(j)["lane"] == "hold"                      # GR: human lane disabled until the source is stored
    assert compliance.queue_human()["queued"] == 0 and "disabled_until_source" in compliance.queue_human()["note"]


def test_human_lane_only_when_law_pack_and_owner_allow(monkeypatch):
    pk = law.pack("GR")
    pk["lanes"]["human_calls"] = "allowed_unless_opt_out"
    monkeypatch.setattr(compliance, "_pack", lambda s=None: pk)
    j = add("NoPhone", {"automated_calls": None}, phone="")
    assert lane_of(j)["lane"] == "hold"                      # owner has not said yes
    db.set_slot("human_calls_allowed", True, "owner", "confirmed")
    assert lane_of(j)["lane"] == "human_call"
    out = compliance.queue_human()
    assert out["queued"] == 1 and db.one("SELECT * FROM handoff WHERE reason='human_lane'")["context"]["customer"] == "NoPhone"
    assert compliance.queue_human()["queued"] == 0           # not twice


def test_unverified_law_item_never_allows_a_lane(monkeypatch):
    pk = law.pack("GR")
    for i in pk["laws"]:
        if i["id"] == "eu-eprivacy-13-2":
            i["verified"] = False
    monkeypatch.setattr(compliance, "_pack", lambda s=None: pk)
    assert lane_of(add("B", {"automated_calls": None}))["lane"] == "hold"


def test_lanes_summary_counts_and_euros():
    people()
    s = compliance.lanes_summary()
    assert s["total"] == 9 and sum(s["counts"].values()) == 9
    assert s["counts"]["ai_call"] == 2 and s["counts"]["consent_request"] == 2
    assert s["eur"]["consent_request"] == 500 and s["eur"]["ai_call"] == 200
    assert s["rules"]["hold"]["do_not_call"] == 1
    assert compliance.lanes_summary([])["total"] == 0


def test_gaps_question_shape_and_content():
    assert compliance.gaps() == []                           # no file, no questions
    people()
    db.insert("doc", name="oroi.md", kind="terms", text="Καλούμε μόνο πελάτες που έχουν δώσει τη συναίνεσή τους για τηλεφωνική επικοινωνία.",
              created_at=db.now_iso())
    g = {x["id"]: x for x in compliance.gaps()}
    q = g["q_consent"]
    assert q["fact"] == "consent_basis" and q["law_ref"] == "gr-3471-11" and q["free_text"] is True
    assert {o["id"] for o in q["options"]} == {"all", "some", "none"}
    assert q["impact"]["customers"] == 4 and q["impact"]["eur"] == 500 + 0 + 100 + 100    # mark 200 + blank 300 + 'optout' 100 + nocontact 100
    assert "telephone contact" in q["why"] or "τηλεφων" in q["why"]
    assert "effect" in q["default_if_skipped"] and "value" in q["default_if_skipped"]
    assert "q_identity" not in g and "q_country" not in g
    db.set_slot("consent_basis", {"automated_calls": "some"}, "owner", "confirmed")
    g = {x["id"]: x for x in compliance.gaps()}
    assert "q_consent" not in g and "q_consent_marks" in g and g["q_consent_marks"]["impact"]["customers"] == 1
    db.set_slot("consent_basis", {"automated_calls": "some", "marks_mean_yes": True}, "owner", "confirmed")
    assert not compliance.gaps()


def test_gap_identity_that_verifies_nothing_and_unknown_country():
    people()
    db.set_slot("consent_basis", {"automated_calls": "all", "marks_mean_yes": True}, "owner", "confirmed")
    db.set_slot("identity_check", {"field": "name", "question": "Μιλάω με τον ασφαλισμένο;"}, "owner")
    db.q("DELETE FROM policy_slot WHERE key='country'")
    g = {x["id"]: x for x in compliance.gaps()}
    assert {"q_identity", "q_country"} <= set(g)
    assert g["q_identity"]["options"][0]["id"] == "birth_year" and g["q_identity"]["fact"] == "identity_check"
    db.set_slot("identity_check", {"field": "plate", "question": "?"}, "owner")           # no customer has this field
    assert "q_identity" in {x["id"] for x in compliance.gaps()}
    db.set_slot("identity_check", {"field": "birth_year", "question": "?"}, "owner")
    assert "q_identity" not in {x["id"] for x in compliance.gaps()}


def test_attest_all_never_touches_a_refusal():
    ids = people()
    out = compliance.attest_all("owner")
    assert out["updated"] == 3                                   # blank, optout row, nocontact (all None, not do_not_call)
    assert db.get("customer", ids["no"])["consent"]["automated_calls"] is False
    assert db.get("customer", ids["blank"])["consent"]["source"] == "attested: owner"


def test_send_consent_requests_web_links_only_no_sms(monkeypatch):
    from prova import flow
    for name in ("_send_sms", "_sms_target"):
        monkeypatch.setattr(flow, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMS path reached")), raising=False)
    ids = people()
    out = compliance.send_consent_requests()
    assert out["channel"] == "web" and out["sent"] == 2
    rows = db.q("SELECT * FROM consent_request ORDER BY id")
    assert {r["customer_id"] for r in rows} == {ids["mark"], ids["blank"]} and all(r["channel"] == "web" and r["status"] == "sent" for r in rows)
    assert all(l["url"].startswith("/k/") and l["inbox_url"].startswith("/inbox/") for l in out["links"])
    assert len({r["token"] for r in rows}) == 2 and all(len(r["token"]) >= 16 for r in rows)
    assert len(db.q("SELECT * FROM event WHERE type='consent'")) == 2
    again = compliance.send_consent_requests()
    assert again["sent"] == 2 and all(l.get("reused") for l in again["links"]) and db.one("SELECT COUNT(*) n FROM consent_request")["n"] == 2
    assert compliance.send_consent_requests([ids["yes"]])["sent"] == 0
    assert db.q("SELECT * FROM cost WHERE kind='sms'") == []


def token_for(i):
    compliance.send_consent_requests([i])
    return db.one("SELECT token FROM consent_request WHERE customer_id=?", (i,))["token"]


def test_consent_page_and_invalid_tokens():
    i = add("Page One", {"automated_calls": None})
    t = token_for(i)
    p = compliance.consent_page(t)
    assert p["customer_name"] == "Page One" and p["text_version"] == compliance.CONSENT_VERSION and p["language"] == "el"
    assert p["company"] == "Ασφάλειες Παπαδάκης" and "τεχνητή νοημοσύνη" in p["text"] and p["inbox_url"] == f"/inbox/{t}"
    for bad in ("", "short", "x" * 20, None):
        assert "error" in compliance.consent_page(bad) and "error" in compliance.consent_answer(bad, "call_now")
    db.set_clock("2026-12-20T11:00")                                                     # 34 days later
    assert "error" in compliance.consent_page(t) and "error" in compliance.consent_answer(t, "call_now")


def test_call_now_records_consent_and_moves_the_lane():
    i = add("Tap Me", {"automated_calls": None})
    t = token_for(i)
    assert lane_of(i)["lane"] == "consent_request"
    r = compliance.consent_answer(t, "call_now")
    assert r["ok"] and r["status"] == "call_now" and r["customer_id"] == i and r["start_call"] and r["ws"] == f"/api/phone/ws?ct={t}"
    c = db.get("customer", i)
    assert c["consent"]["automated_calls"] is True and c["consent"]["source"] == "tap" and c["consent"]["request_id"]
    assert lane_of(i)["lane"] == "ai_call"
    assert db.one("SELECT status FROM consent_request WHERE token=?", (t,))["status"] == "call_now"
    ev = [e["payload"] for e in db.q("SELECT payload FROM event WHERE type='consent'")]
    assert [e["kind"] for e in ev] == ["requested", "given"] and ev[1]["text_hash"] and ev[1]["customer_id"] == i
    db.set_clock("2026-11-15T23:30")                                                     # Sunday night: a fresh tap may be called
    db.update("customer", i, consent={**c["consent"], "at": db.now_iso()})
    assert policy.may_dial(db.get("customer", i), "renew")["allow"]


def test_scheduled_and_stop():
    a, b = add("Sched", {"automated_calls": None}), add("Stopper", {"automated_calls": None})
    ta, tb = token_for(a), token_for(b)
    assert "error" in compliance.consent_answer(ta, "scheduled")                         # needs a time
    r = compliance.consent_answer(ta, "scheduled", "2026-11-17T10:00:00")
    assert r["status"] == "scheduled" and r["scheduled_for"] == "2026-11-17T10:00"
    f = db.one("SELECT * FROM followup WHERE customer_id=?", (a,))
    assert f["status"] == "pending" and f["due_at"].startswith("2026-11-17T10:00")
    assert db.get("customer", a)["consent"]["source"] == "tap"
    r = compliance.consent_answer(tb, "stop")
    assert r["status"] == "stopped"
    c = db.get("customer", b)
    assert c["status"] == "do_not_call" and c["consent"]["automated_calls"] is False and lane_of(b)["lane"] == "hold"
    assert db.q("SELECT payload FROM event WHERE type='consent'")[-1]["payload"]["kind"] == "withdrawn"
    assert "error" in compliance.consent_answer(tb, "call_now")                          # opted out: no tap brings them back
    assert "error" in compliance.consent_answer(ta, "bogus")


def test_join_creates_customer_with_demo_policy_and_records_tap():
    r = compliance.join("Νίκη Δοκιμή", "+30 690 123 4567", "en", compliance.CONSENT_VERSION)
    assert r["ok"] and r["status"] == "call_now" and r["ws"].startswith("/api/phone/ws?ct=")
    c = db.get("customer", r["customer_id"])
    assert c["name"] == "Νίκη Δοκιμή" and c["fields"]["language"] == "en" and c["fields"]["expiry_date"] == "2026-11-19"   # clock + 3 days
    assert c["fields"]["product"] == "Βασικό" and c["consent"]["source"] == "tap" and lane_of(c["id"])["lane"] == "ai_call"
    assert policy.call_language(c) == "en"
    assert compliance.join("", "123456", "el", "")["error"] and compliance.join("A", "12", "el", "")["error"]
    assert "error" in compliance.join("A", "+306900000000", "el", "old-version")
    again = compliance.join("Νίκη Δοκιμή", "+306901234567", "el", "")
    assert again["customer_id"] == r["customer_id"] and db.one("SELECT COUNT(*) n FROM customer")["n"] == 1


def test_brief_labels_unverified_and_has_lanes():
    people()
    b = compliance.brief()
    by = {i["id"]: i for i in b["laws"]}
    assert by["gr-3471-11"]["verified"] is False and by["gr-3471-11"]["label"] == "unverified, confirm with counsel"
    assert by["eu-eprivacy-13"]["verified"] is True and "gr-3471-11" in b["unverified_law_items"]
    assert all(i["quote"] and i["source_id"] for i in b["laws"])
    assert any(i["id"] == "eu-idd-20" for i in b["laws"])                                # insurance business: the IDD items show
    assert b["lanes"]["total"] == 9 and b["lane_rules"]["human_call"].endswith("disabled_until_source")
    assert "Not legal advice" in b["disclaimer"] and b["country"] == "GR"
    db.set_slot("consent_basis", {"automated_calls": "all"}, "owner: Nikos", "confirmed")
    b = compliance.brief()
    assert b["unchecked_attestations"] and "clause" in b["unchecked_attestations"][0]
    db.set_slot("business_type", "gym", "t")
    db.set_slot("company_name", "FitLife", "t")
    assert not any(i["id"] == "eu-idd-20" for i in compliance.brief()["laws"])
