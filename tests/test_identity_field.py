from prova import db, gateway, policy


def _cust(fields, extra=None):
    f = dict(fields)
    if extra:
        f["extra"] = extra
    return {"id": 1, "name": "Test", "fields": f, "notes": ""}


def _setup(monkeypatch):
    monkeypatch.setattr(db, "slots", lambda: {"identity_check": {"field": "Πινακίδα", "question": "Ποια είναι η πινακίδα σας;"},
                                              "language": "el", "company_name": "Acme"})


def test_health_customer_gets_expiry_question(monkeypatch):
    _setup(monkeypatch)
    c = _cust({"expiry_date": "2026-01-15", "language": "el"})
    field, q = policy.identity_field_for(c, "el")
    assert field == "expiry_date" and "λήγει" in q
    assert gateway.loose_match("15 Ιανουαρίου", "2026-01-15", field)
    assert gateway.loose_match("15/1", "2026-01-15", field)
    assert not gateway.loose_match("16 Ιανουαρίου", "2026-01-15", field)


def test_car_customer_keeps_plate(monkeypatch):
    _setup(monkeypatch)
    c = _cust({"expiry_date": "2026-01-15"}, {"Πινακίδα": "ΙΚΧ 1234"})
    assert policy.identity_field_for(c, "el")[0] == "Πινακίδα"


def test_opening_greek_only(monkeypatch):
    _setup(monkeypatch)
    ok = "Καλημέρα, είμαι ψηφιακός βοηθός με τεχνητή νοημοσύνη της Acme. Μιλάω με τον κύριο Νίκο;"
    assert policy.opening_ok(ok, "Acme", "el")
    assert not policy.opening_ok("Καλημέρα, είμαι AI assistant της Acme. Μιλάω με τον Νίκο;", "Acme", "el")


def test_bare_yes_denied_not_counted(monkeypatch):
    _setup(monkeypatch)
    c = _cust({"expiry_date": "2026-01-15"}, {"Πινακίδα": "ΙΚΧ 1234"})
    c["name"] = "Ζήσης Τρικαλιώτης"
    assert not gateway._has_value("Ναι, ο ίδιος είναι.", c)
    assert not gateway._has_value("Ναι, είμαι ο Ζήσης Τρικαλιώτης", c)
    assert gateway._has_value("ΙΚΧ 1234", c)
    g = gateway.CallCtx.__new__(gateway.CallCtx)
    g.customer, g.verified, g.id_failures = c, False, 0
    r = g._perform("verify_identity", {"answer": "Ναι, ο ίδιος είναι."})
    assert r["rule"] == "identity_question_not_asked" and "Ask the identity question first" in r["next"]
    assert g.id_failures == 0 and not g.verified
    assert g._perform("verify_identity", {"answer": "ΙΚΧ 1234"})["verified"] is True
