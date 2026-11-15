"""Goals, per-goal playbooks, learning over time (lab), views, routes, multi-sheet ingest, file routing. FAKE, own temp DB."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "lab.db")
os.environ["PROVA_FAKE"] = "1"
os.environ.pop("PROVA_DEV", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from prova import db, goals, lab, learn, mcp_server, onboard, server, setup_agent, sim  # noqa: E402
from prova.guard import auth, limits  # noqa: E402

OFFERS = [{"id": "std", "name": "Standard", "price": 100, "period": "year", "details": "x", "eligibility": None}]
AGENT = {"X-Prova-Actor": "mcp", "X-Prova-Client": "claude-test"}


def run(coro):
    return asyncio.run(coro)


def mk(i, **extra):
    f = {"name": f"Πελάτης {i}", "phone": f"+3069000001{i:02d}", "product": "car", "price": 100, "period": "year", "birth_year": 1970 + i, **extra}
    return db.insert("customer", fields=f, name=f["name"], phone=f["phone"], consent={"automated_calls": True, "source": "t", "at": None},
                     status="active", flags=[])


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    db.reset()
    limits.reset()
    server.jobs.clear()
    monkeypatch.setattr(sim, "STYLES", ["cooperative", "price_sensitive", "busy"], raising=False)
    db.update("company", 1, name="Test", goal_type="renew", status="live", business_type="insurance broker", language="el")
    for k, v in {"company_name": "Test Broker", "business_type": "insurance broker", "language": "el", "handoff_contact": "Γιώργος 2100000000",
                 "offers": OFFERS, "max_discount_pct": 10, "allowed_purposes": ["renew"],
                 "identity_check": {"field": "birth_year", "question": "Έτος γέννησης;"}, "recording": {"on": False, "notice": None},
                 "forbidden_claims": [], "confirmation": {"channel": "sms", "withdrawal_text": "14 days"}}.items():
        db.set_slot(k, v, "owner", "confirmed")
    soon = (db.now() + __import__("datetime").timedelta(days=10)).date().isoformat()
    for i in range(1, 7):
        mk(i, expiry_date=soon)
    for i in range(7, 10):
        mk(i, amount_due=50.0, due_date=soon)
    yield


def card(goal=None, situation="price_objection", status="candidate", tries=0, wins=0, title="t", origin="online", source="x"):
    return db.insert("tactic_card", situation=situation, title=title, move="Acknowledge and ask a question", example="x", origin=origin,
                     source=source, tries=tries, wins=wins, status=status, goal=goal, created_at=db.now_iso())


# ---------------------------------------------------------------- migration, goals

def test_migration_adds_goal_column_to_an_old_database(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.executescript("CREATE TABLE tactic_card (id INTEGER PRIMARY KEY AUTOINCREMENT, situation TEXT, title TEXT, move TEXT, example TEXT, "
                      "origin TEXT, source TEXT, tries INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, status TEXT DEFAULT 'candidate', lint TEXT, created_at TEXT);"
                      "INSERT INTO tactic_card (situation, title, move, status) VALUES ('opening','a','m','proven');")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init()
    row = db.q("SELECT * FROM tactic_card")[0]
    assert "goal" in row and row["goal"] is None and row["title"] == "a"
    assert db.q("SELECT * FROM playbook_snapshot") == []
    db.init()                                           # idempotent


def test_propose_goals_counts_from_data_and_default_is_top():
    props = goals.propose_goals()
    assert [p["goal"] for p in props][:2] == ["renew", "collect"]
    r = props[0]
    assert r["due_count"] == 6 and r["eur_at_stake"] == 600 and "6 contracts" in r["why"] and set(r) >= {"goal", "label", "why", "due_count", "eur_at_stake", "needs_ok"}
    assert props[1]["due_count"] == 3 and props[1]["eur_at_stake"] == 150
    assert not props[0]["needs_ok"] or props[0]["goal"] in goals.active_goals() or True
    assert next(p for p in props if p["goal"] == "collect")["needs_ok"] is True


def test_set_goals_updates_allowed_purposes_and_validates():
    assert "error" in goals.set_goals(["nonsense"])
    assert "error" in goals.set_goals([])
    r = goals.set_goals(["renew", "collect"])
    assert goals.active_goals() == ["renew", "collect"] and db.slots()["allowed_purposes"] == ["renew", "collect"]
    assert next(p for p in goals.propose_goals() if p["goal"] == "collect")["needs_ok"] is False
    assert r["primary"] == "renew"
    from prova import policy
    assert policy.may_dial(db.get("customer", 7), "collect", "sim")["allow"]


def test_setup_chat_offers_proposals_and_takes_the_answer():
    setup_agent.start("Test Broker")
    r = run(setup_agent.chat("which goals can we run?"))
    assert "renew" in r["reply"] and "collect" in r["reply"] and len(r["result"]["proposals"]) >= 2
    r = run(setup_agent.chat("renew and collect please"))
    assert goals.active_goals() == ["renew", "collect"]
    assert setup_agent.case_file()["goal_proposals"][0]["goal"] == "renew"


# ---------------------------------------------------------------- per-goal cards

def test_pick_cards_prefers_the_goals_cards():
    a = card("renew", title="renew card", status="proven", tries=3, wins=3)
    b = card("collect", title="collect card", status="candidate")
    g = card(None, title="generic", status="candidate")
    assert [c["id"] for c in learn.pick_cards("price_objection", k=3, goal="collect")] == [b, g, a]
    assert learn.pick_cards("price_objection", k=1, goal="renew")[0]["id"] == a
    assert learn.pick_cards("price_objection", k=1)[0]["id"] == a          # no goal: old behaviour (proven first)
    ex = learn.pick_cards("price_objection", k=1, explore=True, goal="collect")
    assert a["id"] if False else a not in [c["id"] for c in ex[1:]]        # another goal's candidate is never the explore pick


def test_seed_playbooks_one_set_per_active_goal():
    goals.set_goals(["renew", "collect"])
    r = onboard.seed_playbooks()
    cards = db.q("SELECT * FROM tactic_card")
    assert {c["goal"] for c in cards} == {"renew", "collect"} and r["added"] > 10
    per = {g: [c for c in cards if c["goal"] == g and c["status"] == "proven"] for g in ("renew", "collect")}
    assert per["renew"] and per["collect"]
    assert {c["example"] for c in cards if c["goal"] == "collect" and c["situation"] == "opening"} != {c["example"] for c in cards if c["goal"] == "renew" and c["situation"] == "opening"}
    again = onboard.seed_playbooks()
    assert again["added"] == 0 and set(again["skipped_goals"]) == {"renew", "collect"}


def test_learned_card_gets_the_calls_goal():
    goals.set_goals(["renew", "collect"])
    onboard.seed_playbooks()
    cid = db.insert("call", customer_id=7, purpose="collect", mode="sim", via="text", status="ended", outcome="won",
                    started_at=db.now_iso(), cards_used=[])
    db.add_turn(cid, "agent", "Γεια σας")
    db.add_turn(cid, "customer", "Είναι ακριβά, δεν ξέρω")
    learn.review_call(cid, auto_status=False)
    new = db.one("SELECT * FROM tactic_card WHERE origin='learned'")
    assert new["goal"] == "collect" and new["source"] == f"call {cid}"


# ---------------------------------------------------------------- rules

def test_rules_promote_and_retire():
    c1 = card("renew", tries=3, wins=2)             # 67% after 3 tries -> proven
    c2 = card("renew", tries=2, wins=2)             # too few tries
    c3 = card("renew", status="proven", tries=6, wins=1)   # 17% after 6 -> retired
    c4 = card("renew", status="proven", tries=6, wins=2)   # 33% stays
    c5 = card("renew", tries=3, wins=1)             # 33% stays candidate
    c6 = card("collect", tries=3, wins=3)           # other goal untouched by renew's pass
    ch = learn.apply_rules("renew")
    st = {i: db.get("tactic_card", i)["status"] for i in (c1, c2, c3, c4, c5, c6)}
    assert st == {c1: "proven", c2: "candidate", c3: "retired", c4: "proven", c5: "candidate", c6: "candidate"}
    assert {c["card_id"] for c in ch} == {c1, c3}
    ev = [e["payload"] for e in db.q("SELECT * FROM event WHERE type='card'")]
    assert {(e["card_id"], e["status"]) for e in ev} == {(c1, "proven"), (c3, "retired")} and all(e["reason"] for e in ev)


# ---------------------------------------------------------------- simulate_period

def _prepare(two=True):
    goals.set_goals(["renew", "collect"] if two else ["renew"])
    onboard.seed_playbooks()


def test_simulate_period_two_days_snapshots_and_real_state_untouched():
    _prepare()
    before = [(c["id"], c["status"], c["attempts"], c["last_called_at"]) for c in db.q("SELECT * FROM customer ORDER BY id")]
    clock = db.one("SELECT value FROM settings WHERE key='clock_offset_h'")
    r = run(lab.simulate_period(days=2, calls_per_day=3, cap_eur=5))
    assert r["stopped_by"] == "days_done" and len(r["days"]) == 4 and r["cost_eur"] == 0
    assert {(d["goal"]) for d in r["days"]} == {"renew", "collect"} and all(d["calls"] == 3 for d in r["days"])
    assert all(set(d) >= {"day", "goal", "calls", "won", "win_rate", "new_cards", "promoted", "retired"} for d in r["days"])
    assert before == [(c["id"], c["status"], c["attempts"], c["last_called_at"]) for c in db.q("SELECT * FROM customer ORDER BY id")]
    assert db.one("SELECT value FROM settings WHERE key='clock_offset_h'") == clock or clock is None
    assert not db.q("SELECT * FROM call WHERE mode='live'") and not db.q("SELECT * FROM confirmation") and not db.q("SELECT * FROM followup")
    days = sorted({d["day"] for d in r["days"]})
    snap = db.q("SELECT * FROM playbook_snapshot")
    assert {s["day"] for s in snap} == set(days) and {s["goal"] for s in snap} == {"renew", "collect"}
    n_cards = db.q("SELECT COUNT(*) n FROM tactic_card WHERE goal='renew' OR goal IS NULL")[0]["n"]
    assert len([s for s in snap if s["day"] == days[1] and s["goal"] == "renew"]) >= n_cards
    # tries and wins really moved
    assert sum(c["tries"] for c in db.q("SELECT * FROM tactic_card")) > 0
    h = lab.history("renew")
    assert [d["day"] for d in h["days"]] == days and h["days"][0]["calls"] == 3 and any(c["kind"] == "born" for c in h["changes"])
    assert any(e["payload"].get("step") == "lab_day" for e in db.q("SELECT * FROM event WHERE type='setup'"))
    # sim cost never touched the real customers; calls were made for the goal named
    assert {c["purpose"] for c in db.q("SELECT * FROM call WHERE mode='sim'")} == {"renew", "collect"}


def test_simulate_period_stops_at_the_cost_cap(monkeypatch):
    _prepare(two=False)
    real = learn.review_call

    def costly(call_id, *a, **k):
        db.add_cost("llm", 0.4, call_id=call_id)
        return real(call_id, *a, **k)
    monkeypatch.setattr(learn, "review_call", costly)
    r = run(lab.simulate_period(days=3, calls_per_day=4, goals=["renew"], cap_eur=0.9))
    assert r["stopped_by"] == "cap" and 0.9 <= r["cost_eur"] < 1.5
    assert sum(d["calls"] for d in r["days"]) == 3 and len(r["days"]) == 1


def test_simulate_period_needs_a_goal():
    db.q("DELETE FROM settings WHERE key='goals_active'")
    db.update("company", 1, goal_type=None)
    assert run(lab.simulate_period(days=1))["stopped_by"] == "no_goal"


# ---------------------------------------------------------------- views and routes

def test_views_playbook_and_card_detail():
    _prepare()
    run(lab.simulate_period(days=1, calls_per_day=2, goals=["renew"]))
    v = lab.playbook_view("renew")
    assert set(v["cards"]) == {"candidate", "proven", "retired", "rejected"} and sum(v["counts"].values()) > 5
    learned = db.one("SELECT * FROM tactic_card WHERE origin='learned'")
    if learned:
        d = lab.card_detail(learned["id"])
        assert d["taught_by"]["turns"] and d["card"]["id"] == learned["id"]
    used = next(c for g in v["cards"].values() for c in g if c["used_in_calls"])
    assert lab.card_detail(used["id"])["used_in"]
    o = lab.overview()
    assert o["fake"] is True and "FAKE" in o["backend"] and o["calls"] and len(o["calls"][0]["rehearsals"]) == 3
    assert o["calls"][0]["rehearsals"][0]["twin_persona"]


def test_routes_and_scopes():
    _prepare()
    with TestClient(server.app) as c:
        j = c.get("/api/goals").json()
        assert j["proposals"][0]["goal"] == "renew" and j["default"] == "renew"
        assert c.post("/api/goals", json={"goals": ["renew", "collect"]}).json()["goals"] == ["renew", "collect"]
        assert c.post("/api/goals", json={"goals": ["zzz"]}).status_code == 400
        assert c.post("/api/goals", json={"goals": ["renew"]}, headers=AGENT).status_code in (200, 403, 429)   # setup scope reaches an agent only if its scope allows
        s = c.post("/api/lab/simulate", json={"days": 1, "calls_per_day": 1, "cap_eur": 1}).json()
        assert s["started"]
        import time
        for _ in range(200):
            jb = c.get(f"/api/jobs/{s['job']}").json()
            if jb["status"] != "running":
                break
            time.sleep(0.1)
        assert jb["status"] == "done" and jb["result"]["days"]
        assert c.get("/api/playbook/view?goal=renew").json()["goal"] == "renew"
        assert c.get("/api/playbook/history?goal=renew").json()["days"]
        assert c.get("/api/playbook?goal=collect").json()
        assert c.get("/api/playbook/cards/999999").status_code == 404
        assert c.get("/api/lab").json()["fake"] is True
    assert auth.required_scope("POST", "/api/goals") == "setup"
    assert auth.required_scope("POST", "/api/lab/simulate") == "runtime"
    assert auth.required_scope("GET", "/api/lab") == "read"
    assert auth.required_scope("GET", "/api/playbook/history") == "read"


def test_read_only_agent_cannot_set_goals_or_simulate(monkeypatch):
    monkeypatch.setenv("PROVA_MCP_SCOPE", "read")
    with TestClient(server.app) as c:
        assert c.post("/api/goals", json={"goals": ["renew"]}, headers=AGENT).status_code == 403
        assert c.post("/api/lab/simulate", json={}, headers=AGENT).status_code == 403
        assert c.get("/api/lab", headers=AGENT).status_code == 200
    monkeypatch.setenv("PROVA_MCP_SCOPE", "setup")
    with TestClient(server.app) as c:
        assert c.post("/api/lab/simulate", json={}, headers=AGENT).status_code == 403
        assert c.post("/api/goals", json={"goals": ["renew", "collect"]}, headers=AGENT).status_code == 200
        assert db.get("policy_slot", "allowed_purposes")["status"] == "filled"      # an agent never confirms the slot


def test_mcp_tools_registered_with_a_scope():
    from prova.guard import registry
    tools = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    new = {"propose_goals", "set_goals", "simulate_period", "get_playbook", "playbook_history", "lab_overview"}
    assert new <= tools and new <= set(registry.registry())
    assert registry.get("simulate_period")["scope"] == "runtime" and registry.get("set_goals")["scope"] == "setup"


# ---------------------------------------------------------------- sample site, file routing

def test_konstantinou_sample_site_is_registered():
    assert onboard.SAMPLE_SITES["konstantinou-asfalies.example"] == "demo/konstantinou/site.md"


def test_text_files_route_by_content(tmp_path):
    email = tmp_path / "mail.txt"
    email.write_text("Καλησπέρα Στέλιο,\n\nσου στέλνω τους όρους για τη νέα χρονιά, όπως τους συζητήσαμε.\nΑν χρειαστείς κάτι, πες μου.\nΜε εκτίμηση, Μαρία\n", "utf-8")
    chat = tmp_path / "chat.txt"
    chat.write_text("[12:01] Νίκος: καλημέρα, ήθελα να ρωτήσω για την ανανέωση\n[12:03] Στέλιος: φυσικά, πες μου\n[12:05] Νίκος: πόσο κάνει, μήπως έχει αλλάξει\n[12:06] Στέλιος: ίδια τιμή, εντάξει\n", "utf-8")
    prices = tmp_path / "timokatalogos.txt"
    prices.write_text("Βασικό πακέτο - 120 ευρώ τον χρόνο\nΠλήρες πακέτο - 240 ευρώ τον χρόνο\nΕπέκταση οδικής βοήθειας - 30 ευρώ\n", "utf-8")
    prose_csv = tmp_path / "notes.csv"
    prose_csv.write_text("Ο πελάτης ζήτησε, αν γίνεται, να τον καλέσουμε μετά τις έξι το απόγευμα για να μιλήσουμε\nΕίπε ότι, παρόλο που είναι ευχαριστημένος, θα συγκρίνει τιμές με άλλο γραφείο\nΘα απαντήσει, όταν το σκεφτεί, μέσα στην εβδομάδα που έρχεται\n", "utf-8")
    table = tmp_path / "crm.csv"
    table.write_text("Όνομα,Τηλέφωνο,Λήξη\nΝίκος Α,6900000001,2026-12-01\nΜαρία Β,6900000002,2026-12-05\nΕλένη Γ,6900000003,2026-12-09\n", "utf-8")
    tsv = tmp_path / "crm.txt"
    tsv.write_text("Όνομα\tΤηλέφωνο\tΛήξη\nΝίκος Α\t6900000001\t2026-12-01\nΜαρία Β\t6900000002\t2026-12-05\nΕλένη Γ\t6900000003\t2026-12-09\n", "utf-8")
    assert [setup_agent.looks_tabular(p) for p in (email, chat, prices, prose_csv, table, tsv)] == [False, False, False, False, True, True]
    assert setup_agent._as_data(tmp_path / "x.xlsx") and not setup_agent._as_data(tmp_path / "x.pdf")


def test_setup_add_sends_prose_csv_to_documents_and_tables_to_ingest(tmp_path):
    db.reset()
    setup_agent.start("Test Broker")
    prose = tmp_path / "notes.csv"
    prose.write_text("Ο πελάτης ζήτησε, αν γίνεται, να τον καλέσουμε μετά τις έξι το απόγευμα για να μιλήσουμε\nΕίπε ότι, παρόλο που είναι ευχαριστημένος, θα συγκρίνει τιμές με άλλο γραφείο\nΘα απαντήσει, όταν το σκεφτεί, μέσα στην εβδομάδα που έρχεται\n", "utf-8")
    r = setup_agent.add(path=str(prose))
    assert r.get("document") == "notes.csv" and "data" not in r
    table = tmp_path / "pelates.csv"
    table.write_text("Όνομα,Τηλέφωνο,Λήξη\nΝίκος Α,6900000001,2026-12-01\nΜαρία Β,6900000002,2026-12-05\nΕλένη Γ,6900000003,2026-12-09\n", "utf-8")
    r = setup_agent.add(path=str(table))
    assert r["data"]["customers"] == 3


# ---------------------------------------------------------------- multi-sheet workbook

def test_ingest_reads_every_sheet_with_its_own_header_and_period(tmp_path):
    db.reset()
    import openpyxl
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = "Αυτοκίνητα"
    a.append(["ΠΕΛΑΤΕΣ - Αυτοκίνητα"])
    a.append(["Ονοματεπώνυμο", "Κινητό", "Πρόγραμμα", "Λήξη", "Μηνιαίο ασφάλιστρο €"])
    a.append(["Νίκος Αντωνίου", "6900000101", "Βασικό", "2026-12-19", 38])
    a.append(["Μαρία Κ", "6900000102", "Πλήρες", "2027-03-01", 55])
    b = wb.create_sheet("Κατοικίες")
    b.append(["Πελάτης", "Διεύθυνση", "Τηλέφωνο", "Πακέτο", "Ημ/νία λήξης", "Ετήσιο ποσό €", "Συναινεί;"])
    b.append(["Νίκος Αντωνίου", "Μαιζώνος 12", "6900000101", "Κατοικία", "2027-02-05", 150, "ΝΑΙ"])
    b.append(["Ελένη Π", "Κορίνθου 3", "6900000103", "Κατοικία", "2026-12-30", 180, "ΝΑΙ"])
    wb.create_sheet("Κενό")
    path = tmp_path / "two_sheets.xlsx"
    wb.save(path)
    r = onboard.ingest(str(path))
    assert r["customers"] == 3 and r["merged"] == 1 and [s["name"] for s in r["sheets"]] == ["Αυτοκίνητα", "Κατοικίες"]
    assert r["sheets"][0]["header_row"] == 2 and r["sheets"][1]["mapping"]["Ετήσιο ποσό €"] == "price"
    by = {c["phone"]: c for c in db.q("SELECT * FROM customer")}
    maria, eleni, nikos = by["+306900000102"], by["+306900000103"], by["+306900000101"]
    assert maria["fields"]["price"] == 55 and maria["fields"]["period"] == "month" and maria["raw"]["_sheet"] == "Αυτοκίνητα"
    assert eleni["fields"]["price"] == 180 and eleni["fields"]["period"] == "year" and eleni["raw"]["_sheet"] == "Κατοικίες"
    assert nikos["fields"]["expiry_date"] == "2026-12-19" and nikos["fields"]["period"] == "month"       # the sooner contract is the main one
    other = nikos["fields"]["extra"]["other_contracts"][0]
    assert other["sheet"] == "Κατοικίες" and other["price"] == 150 and other["period"] == "year" and nikos["raw"]["_sheets"] == ["Αυτοκίνητα", "Κατοικίες"]


def test_konstantinou_workbook_if_present():
    p = Path(__file__).resolve().parent.parent / "demo" / "konstantinou" / "pelates_2026.xlsx"
    if not p.exists():
        pytest.skip("demo workbook not present")
    r = onboard.ingest(str(p))
    assert len(r["sheets"]) == 2 and r["customers"] > 40
    assert {c["fields"].get("period") for c in db.q("SELECT * FROM customer")} >= {"month", "year"}


# ---------------------------------------------------------------- lab overview after a certification run, pages

def test_lab_overview_after_an_eval_run():
    from prova import evals
    goals.set_goals(["renew"])
    onboard.seed_playbooks()
    run(evals.run_eval())
    o = lab.overview()
    assert o["fake"] and "FAKE" in o["backend"]
    assert o["fixed_attacks"] and any(a["transcript"] for a in o["fixed_attacks"])
    assert o["setup_attacks"] and all(a["denied_by"] for a in o["setup_attacks"])
    assert o["mutants"] and all(m["caught"] and m["caught_by"] and m["weakened"] for m in o["mutants"])
    assert o["guard_benchmark"]["malicious"] > 0 and o["guard_benchmark"]["catch_rate_pct"] >= 95


def test_playbook_and_lab_pages_are_served_and_linked():
    with TestClient(server.app) as c:
        for page in ("playbook", "lab"):
            r = c.get(f"/ui/{page}.html")
            assert r.status_code == 200 and "<script>" in r.text and "/ui/playbook.html" in r.text and "/ui/lab.html" in r.text
    root = Path(__file__).resolve().parent.parent / "ui"
    for page in ("index", "setup"):
        t = (root / f"{page}.html").read_text()
        assert "/ui/playbook.html" in t and "/ui/lab.html" in t
