import os
import sys
import tempfile
import types
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp()
os.environ["PROVA_DB"] = os.path.join(_tmp, "flow.db")
os.environ["PROVA_OUT"] = os.path.join(_tmp, "out")
os.environ["PROVA_FAKE"] = "1"
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "PHONE_TO", "PHONE_ALLOW_CUSTOMER_NUMBERS"):
    os.environ.pop(k, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from prova import db, flow  # noqa: E402

OFFERS = [{"id": "p1", "name": "Full cover", "price": 200.0, "period": "year"}]


def _fake_policy():
    m = types.ModuleType("prova.policy")
    m.effective = lambda: {"max_attempts": 3, "min_gap_hours": 24}
    m.missing_required = lambda: []
    m.derived_checks = lambda: []

    def may_dial(c, purpose, mode="live"):
        if c["status"] in ("do_not_call", "won", "lost"):
            return {"allow": False, "rule": "status", "reason": "customer status " + c["status"]}
        if (c.get("consent") or {}).get("automated_calls") is not True:
            return {"allow": False, "rule": "consent", "reason": "no recorded consent"}
        return {"allow": True, "rule": "ok", "reason": ""}

    m.may_dial = may_dial
    return m


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    db.reset()
    pol = _fake_policy()
    monkeypatch.setitem(sys.modules, "prova.policy", pol)
    db.set_clock("2026-11-16T10:30")
    db.set_slot("company_name", "Test Broker", "t", "confirmed")
    db.set_slot("language", "el", "t", "confirmed")
    db.set_slot("offers", OFFERS, "t", "confirmed")
    db.set_slot("confirmation", {"channel": "sms", "withdrawal_text": "Μπορείτε να υπαναχωρήσετε εντός 14 ημερών."}, "t", "confirmed")
    db.update("company", 1, goal_type="renew", goal_text="renew", status="live", name="Test Broker")
    yield


def customer(name="Maria", phone="+306900000001", consent=True, **fields):
    return db.insert("customer", name=name, phone=phone, status="active", flags=[], attempts=0,
                     consent={"automated_calls": consent}, fields={"name": name, "phone": phone, **fields}, raw={})


def call(cid, outcome, detail=None, mode="live"):
    return db.insert("call", customer_id=cid, purpose="renew", mode=mode, via="text", status="ended", outcome=outcome,
                     outcome_detail=detail or {}, started_at=db.now_iso())


def test_close_won_live_creates_confirmation():
    cid = customer()
    k = call(cid, "won", {"offer_id": "p1", "discount_pct": 10})
    r = flow.close_call(k)
    assert r["outcome"] == "won" and r["channel"] == "stored"
    assert db.get("customer", cid)["status"] == "pending_confirm"
    conf = db.one("SELECT * FROM confirmation WHERE call_id=?", (k,))
    assert conf["token"] and conf["offer"]["price"] == 180.0
    assert "180.00" in conf["message"] and "υπαναχωρήσετε" in conf["message"] and f"/c/{conf['token']}" in conf["message"]
    assert db.one("SELECT * FROM event WHERE type='confirmation'")["payload"]["status"] == "sent"
    assert db.get("customer", cid)["attempts"] == 1
    assert flow.close_call(k).get("already_closed")


def test_confirm_token_makes_customer_won():
    cid = customer()
    k = call(cid, "won", {"offer_id": "p1"})
    flow.close_call(k)
    token = db.one("SELECT token FROM confirmation")["token"]
    assert flow.confirm(token)["ok"]
    assert db.get("customer", cid)["status"] == "won"
    assert db.one("SELECT status FROM confirmation")["status"] == "confirmed"
    assert flow.confirm("nope")["ok"] is False


def test_dry_run_changes_nothing_outside_call():
    cid = customer()
    for outcome in ("won", "thinking", "human", "declined", "do_not_call"):
        flow.close_call(call(cid, outcome, {"offer_id": "p1"}, mode="sim"))
    c = db.get("customer", cid)
    assert c["status"] == "active" and c["attempts"] == 0
    for t in ("confirmation", "followup", "handoff"):
        assert db.q(f"SELECT * FROM {t}") == []
    won = db.q("SELECT * FROM call WHERE outcome='won'")[0]
    assert "confirmation_preview" in won["outcome_detail"]


def test_followup_per_outcome_and_hours():
    cid = customer()
    flow.close_call(call(cid, "thinking"))
    f = db.one("SELECT * FROM followup WHERE status='pending'")
    assert f["due_at"].startswith("2026-11-18T10:30")  # +2 days, Wednesday, inside hours

    # evening no_answer (+4 h would be 22:30) moves to next morning
    db.set_clock("2026-11-17T18:30")
    cid2 = customer("Nikos", "+306900000002")
    flow.close_call(call(cid2, "no_answer"))
    due = datetime.fromisoformat(db.one("SELECT * FROM followup WHERE customer_id=?", (cid2,))["due_at"])
    assert 9 <= due.hour < 20 and due.isoweekday() != 7 and due > db.now() + timedelta(hours=4)


def test_followup_respects_max_attempts():
    cid = customer()
    db.update("customer", cid, attempts=2)
    r = flow.close_call(call(cid, "callback", {"callback_at": "2026-11-17T12:00:00"}))
    assert "max attempts" in r["followup"]
    assert db.q("SELECT * FROM followup") == []


def test_callback_time_used_and_old_followup_cancelled():
    cid = customer()
    flow.close_call(call(cid, "thinking"))
    flow.close_call(call(cid, "callback", {"callback_at": "2026-11-20T12:00:00"}))
    rows = db.q("SELECT * FROM followup ORDER BY id")
    assert [r["status"] for r in rows] == ["cancelled", "pending"] and rows[1]["due_at"].startswith("2026-11-20T12:00")


def test_human_dnc_declined():
    a, b, c = customer("A", "+306900000011"), customer("B", "+306900000012"), customer("C", "+306900000013")
    flow.close_call(call(a, "human", {"reason": "legal threat"}))
    h = db.one("SELECT * FROM handoff")
    assert h["status"] == "waiting" and h["context"]["customer"] == "A" and h["reason"] == "legal threat"
    flow.close_call(call(b, "thinking"))
    flow.close_call(call(b, "do_not_call"))
    assert db.get("customer", b)["status"] == "do_not_call"
    assert db.q("SELECT * FROM followup WHERE customer_id=? AND status='pending'", (b,)) == []
    flow.close_call(call(c, "declined"))
    assert db.get("customer", c)["status"] == "lost"


def test_sms_target_rules(monkeypatch):
    c = {"phone": "+306900000001"}
    assert flow._sms_target(c) is None
    monkeypatch.setenv("PHONE_TO", "+306999999999")
    assert flow._sms_target(c) == "+306999999999"
    monkeypatch.setenv("PHONE_ALLOW_CUSTOMER_NUMBERS", "1")
    assert flow._sms_target(c) == "+306900000001"


def test_message_english():
    m = flow.confirmation_message({"name": "Ann"}, {"name": "Full cover", "price": 180, "discount_pct": 10, "period": "year"},
                                  "http://x/c/t", "en")
    assert "Full cover" in m and "180.00" in m and "http://x/c/t" in m and "14" not in m.split("http")[0]


def test_rank_customers_fake():
    near = customer("Near", "+306900000021", expiry_date=(db.now() + timedelta(days=3)).date().isoformat())
    far = customer("Far", "+306900000022", expiry_date=(db.now() + timedelta(days=50)).date().isoformat())
    nocons = customer("NoConsent", "+306900000023", consent=False)
    r = flow.rank_customers()
    assert [x["customer_id"] for x in r][:2] == [near, far]
    assert r[-1]["customer_id"] == nocons and r[-1]["score"] is None and "consent" in r[-1]["reason"]
    assert db.get("customer", near)["score"] > db.get("customer", far)["score"]
    assert db.company()["setup"]["plan"] == "done"


def test_report_shape():
    cid = customer()
    k = call(cid, "won", {"offer_id": "p1", "discount_pct": 10})
    db.add_cost("llm", 0.02, call_id=k)
    call(customer("Z", "+306900000099"), "declined")
    r = flow.report()
    for key in ("goal", "customers", "calls", "value_won_eur", "cost_total_eur", "cost_per_call_eur", "cost_per_won_eur",
                "corrections", "handoffs_waiting", "followups_pending", "playbook", "twin_fidelity", "chain"):
        assert key in r
    assert r["value_won_eur"] == 180.0 and r["calls"]["by_outcome"] == {"won": 1, "declined": 1}
    assert r["cost_per_won_eur"] == 0.02 and r["cost_per_call_eur"] == 0.01 and r["chain"]["ok"]


def test_export_results_and_erase():
    cid = customer()
    flow.close_call(call(cid, "won", {"offer_id": "p1"}))
    text = open(flow.export_results(), encoding="utf-8").read()
    assert "Maria" in text and "pending_confirm" in text and "waiting for customer confirmation" in text
    db.add_turn(db.q("SELECT id FROM call")[0]["id"], "customer", "hello")
    r = flow.erase_customer(cid)
    assert r["erased"] and r["removed"]["calls"] == 1 and r["removed"]["confirmations"] == 1 and r["removed"]["turns"] == 1
    assert db.q("SELECT * FROM customer") == [] and db.q("SELECT * FROM turn") == []
    assert db.verify_chain()["ok"] and r["events_retained"] > 0


def test_run_customer_denied_and_failure(monkeypatch):
    import asyncio
    cid = customer(consent=False)
    r = asyncio.run(flow.run_customer(cid, "sim"))
    assert r["denied"] and r["rule"] == "consent"
    ok = customer("Ok", "+306900000031")
    # sim module missing -> never raises, call ends failed
    monkeypatch.setitem(sys.modules, "prova.sim", None)
    r = asyncio.run(flow.run_customer(ok, "sim"))
    assert r["outcome"] == "failed" and "sim" in r["note"]
    assert db.get("call", r["call_id"])["outcome"] == "failed"


def test_run_customer_pipeline_with_fakes(monkeypatch):
    import asyncio
    order = []
    sim = types.ModuleType("prova.sim")

    async def warmup(call_id, n=3):
        order.append("warmup")

    async def run_text_call(call_id, **kw):
        order.append("call")
        db.update("call", call_id, status="ended", outcome="thinking", outcome_detail={})
        return {"turns": 4}

    sim.warmup, sim.run_text_call = warmup, run_text_call
    learn = types.ModuleType("prova.learn")
    learn.review_call = lambda call_id: order.append("review") or {}
    monkeypatch.setitem(sys.modules, "prova.sim", sim)
    monkeypatch.setitem(sys.modules, "prova.learn", learn)
    cid = customer()
    r = asyncio.run(flow.run_customer(cid, "sim"))
    assert order == ["warmup", "call", "review"] and r["outcome"] == "thinking"
    res = asyncio.run(flow.run_campaign(2, "sim", 2))
    assert len(res) == 1  # rank ran, one dialable customer


# ---------------------------------------------------------------- API

@pytest.fixture
def client():
    from prova.server import app
    with TestClient(app) as c:
        yield c


def test_api_status_policy_customers_events(client):
    cid = customer()
    s = client.get("/api/status").json()
    assert s["company"]["status"] == "live" and s["counts"]["customers"]["active"] == 1 and "jobs" in s
    p = client.get("/api/policy").json()
    assert {x["key"] for x in p["slots"]} >= {"offers", "language"} and "floor" in p and p["missing_required"] == []
    cs = client.get("/api/customers").json()
    assert cs[0]["name"] == "Maria" and cs[0]["consent"] == {"automated_calls": True}
    assert client.get(f"/api/customers/{cid}").json()["customer"]["raw"] == {}
    assert client.get("/api/events?after=0").json()
    assert client.get("/api/report").json()["chain"]["ok"]
    assert client.get("/api/calls").json() == []
    assert client.get("/api/ingest").status_code == 200
    assert client.post("/api/clock", json={"advance_hours": 1}).json()["now"]


def test_api_missing_module_is_503(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "prova.onboard", None)
    r = client.post("/api/setup/playbook")
    assert r.status_code == 503 and "onboard" in r.json()["error"]


def test_api_call_job_and_confirmation_page(client, monkeypatch):
    sim = types.ModuleType("prova.sim")

    async def warmup(call_id, n=3):
        return {}

    async def run_text_call(call_id, **kw):
        db.update("call", call_id, status="ended", outcome="declined", outcome_detail={})
        return {"turns": 1}

    sim.warmup, sim.run_text_call = warmup, run_text_call
    monkeypatch.setitem(sys.modules, "prova.sim", sim)
    cid = customer()
    r = client.post("/api/calls", json={"customer_id": cid, "mode": "sim"}).json()
    assert r["started"]
    import time
    for _ in range(50):
        j = client.get(f"/api/jobs/{r['job']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done" and j["result"]["outcome"] == "declined"
    assert any(x["id"] == r["job"] for x in client.get("/api/status").json()["jobs"])

    k = call(cid, "won", {"offer_id": "p1"})
    flow.close_call(k)
    token = db.one("SELECT token FROM confirmation")["token"]
    page = client.get(f"/c/{token}")
    assert page.status_code == 200 and "Επιβεβαιώνω" in page.text and "υπαναχωρήσετε" in page.text
    assert "I confirm" in client.get(f"/c/{token}?lang=en").text
    assert "Ευχαριστούμε" in client.post(f"/c/{token}").text
    assert db.get("customer", cid)["status"] == "won"
    assert client.get("/c/bogus").status_code == 200


def test_api_handoff_resolve_and_erase(client):
    cid = customer()
    flow.close_call(call(cid, "human", {"reason": "x"}))
    h = client.get("/api/handoffs").json()
    assert h[0]["status"] == "waiting"
    client.post(f"/api/handoffs/{h[0]['id']}/resolve", json={"note": "called back"})
    assert client.get("/api/handoffs").json()[0]["status"] == "resolved"
    assert client.delete(f"/api/customers/{cid}").json()["erased"]
