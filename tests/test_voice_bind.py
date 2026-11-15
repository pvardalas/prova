import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "voicebind.db")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from prova import compliance, db, policy, voice  # noqa: E402
from prova.server import app as server_app  # noqa: E402

OFFERS = [{"id": "basic", "name": "Βασικό", "price": 100, "period": "year", "details": "x"}]


@pytest.fixture(autouse=True)
def seed():
    db.reset()
    voice.PHONES.clear()
    db.update("company", 1, name="Παπαδάκης", goal_type="renew", status="live", budget_eur=5.0)
    for k, v in {"company_name": "Ασφάλειες Παπαδάκης", "business_type": "insurance broker", "language": "el", "country": "GR",
                 "handoff_contact": "x 210", "offers": OFFERS, "max_discount_pct": 10, "allowed_purposes": ["renew"],
                 "identity_check": {"field": "birth_year", "question": "?"},
                 "confirmation": {"channel": "sms", "withdrawal_text": "14"}}.items():
        db.set_slot(k, v, "owner", "confirmed")
    db.set_clock("2026-11-16T11:00")


def customer(name, ph):
    f = {"name": name, "phone": ph, "birth_year": 1970, "price": 100, "expiry_date": "2026-12-01", "since_date": "2020-01-01"}
    return db.insert("customer", fields=f, name=name, phone=ph, consent={"automated_calls": None}, status="active", flags=[])


def tap(cid):
    compliance.send_consent_requests([cid])
    t = db.one("SELECT token FROM consent_request WHERE customer_id=?", (cid,))["token"]
    assert compliance.consent_answer(t, "call_now")["ok"]
    return t


def new_call(cid):
    return db.insert("call", customer_id=cid, purpose="renew", mode="live", via="text", status="queued")


def test_socket_needs_a_valid_consent_token_or_the_desk_token():
    a = customer("Α Α", "+306900000001")
    compliance.send_consent_requests([a])
    t = db.one("SELECT token FROM consent_request")["token"]                 # sent, not yet tapped: no socket yet
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as c:
        for url in ("/api/phone/ws", "/api/phone/ws?ct=nope", f"/api/phone/ws?ct={t}", "/api/phone/ws?k=wrong"):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(url) as ws:
                    ws.receive_json()
        compliance.consent_answer(t, "call_now")
        with c.websocket_connect(f"/api/phone/ws?ct={t}") as ws:
            assert ws.receive_json()["type"] == "ready"
            assert [p.customer_id for p in voice.PHONES] == [a]
        with c.websocket_connect(f"/api/phone/ws?k={voice.TOKEN}") as ws:     # the desk phone still works
            assert ws.receive_json()["type"] == "ready"
            assert [p.customer_id for p in voice.PHONES] == [None]


def test_idle_phone_never_returns_a_customer_socket_and_bound_phone_is_per_customer():
    p1, p2, desk = voice.BrowserPhone(None), voice.BrowserPhone(None), voice.BrowserPhone(None)
    p1.closed = p2.closed = desk.closed = False
    p1.customer_id, p2.customer_id = 1, 2
    voice.PHONES.extend([p1, p2, desk])
    assert voice._idle_phone() is desk and voice.bound_phone(1) is p1 and voice.bound_phone(2) is p2 and voice.bound_phone(3) is None
    desk.busy = True
    assert voice._idle_phone() is None                                         # customer sockets are never guessed
    p1.busy = True
    assert voice.bound_phone(1) is None


def test_live_call_rings_the_customers_own_socket_only():
    a, b = customer("Α Α", "+306900000001"), customer("Β Β", "+306900000002")
    ta, tb = tap(a), tap(b)
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as c:
        with c.websocket_connect(f"/api/phone/ws?ct={ta}") as wa, c.websocket_connect(f"/api/phone/ws?ct={tb}") as wb:
            assert wa.receive_json()["type"] == "ready" and wb.receive_json()["type"] == "ready"
            call = new_call(b)
            fut = c.portal.start_task_soon(voice.run_live_call, call)
            ring = wb.receive_json()
            assert ring["type"] == "ring" and ring["call_id"] == call and ring["customer"] == "Β Β"
            wb.send_json({"type": "answer"})
            assert wb.receive_json()["type"] == "connected"
            msgs = []
            while True:
                m = wb.receive_json()
                msgs.append(m)
                if m["type"] == "ended":
                    break
            res = fut.result(timeout=20)
            assert res["via"] == "browser" and db.get("call", call)["via"] == "browser"
            assert [m["role"] for m in msgs if m["type"] == "caption"] and msgs[-1]["type"] == "ended"
            assert any(m["role"] == "agent" for m in msgs if m["type"] == "caption")
        # customer A's socket got nothing but "ready"
    assert not [p for p in voice.PHONES if p.customer_id == a and p.in_call]


def test_tunnel_allows_public_paths_but_not_the_api():
    with TestClient(server_app) as c:
        join = c.get("/join", headers={"cf-ray": "x"})
        assert "not available over the public tunnel" not in join.text
        assert c.get("/api/status", headers={"cf-ray": "x"}).status_code == 403


def test_fake_call_without_a_socket_runs_as_text():
    a = customer("Α Α", "+306900000001")
    tap(a)
    res = asyncio.run(voice.run_live_call(new_call(a)))
    assert res["via"] == "text" and db.get("call", res["call_id"])["via"] == "text"


def test_fake_call_without_answer_ends_no_answer(monkeypatch):
    a = customer("Α Α", "+306900000001")
    t = tap(a)
    monkeypatch.setattr(voice, "FAKE_RING_S", 0.2)
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as c, c.websocket_connect(f"/api/phone/ws?ct={t}") as ws:
        ws.receive_json()
        fut = c.portal.start_task_soon(voice.run_live_call, new_call(a))
        assert ws.receive_json()["type"] == "ring"
        assert ws.receive_json() == {"type": "ended", "outcome": "no_answer"}
        assert fut.result(timeout=20)["via"] == "text"


def test_consent_call_now_then_may_dial_follows_the_fresh_tap_even_at_night():
    a = customer("Α Α", "+306900000001")
    db.set_clock("2026-11-15T23:00")
    assert policy.may_dial(db.get("customer", a), "renew")["rule"] == "consent"
    tap(a)
    assert policy.may_dial(db.get("customer", a), "renew")["allow"]
