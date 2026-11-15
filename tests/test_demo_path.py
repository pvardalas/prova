"""BUILD.md R8: the demo path through the REST API (FAKE, own temp DB), once as the owner and once with an MCP actor for setup.

site (planted instructions) -> pelates.xlsx -> photo truth -> <= 3 questions -> planted instruction denied and logged
-> answers -> build / certify -> turn-on token -> an agent cannot turn on, the owner taps -> lanes -> consent page ->
call now -> consent recorded -> browser call (fake) -> won -> confirmation page. No SMS / Twilio on the way.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "demo_path.db")
os.environ["PROVA_FAKE"] = "1"
os.environ.pop("PROVA_DEV", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from prova import db, flow, intake, mcp_server, server, sim, voice  # noqa: E402
from prova.guard import limits  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INS = ROOT / "samples" / "insurance"
SITE = "https://papadakis-asfalies.example"
AGENT = {"X-Prova-Actor": "mcp", "X-Prova-Client": "claude-test"}
JSON = {"Accept": "application/json"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    db.reset()
    limits.reset()
    server.jobs.clear()
    voice.PHONES.clear()
    monkeypatch.setattr(intake, "intake_dir", lambda: tmp_path / "intake")
    (tmp_path / "intake").mkdir()
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    monkeypatch.delenv("PROVA_DEV", raising=False)
    monkeypatch.delenv("PROVA_TOKEN", raising=False)
    monkeypatch.setattr(voice, "FAKE_RING_S", 20)
    monkeypatch.setattr(sim, "STYLES", ["cooperative"])    # the FAKE twin agrees: the call is won

    def no_sms(*a, **k):
        raise AssertionError("an SMS / Twilio path was reached")
    monkeypatch.setattr(flow, "_send_sms", no_sms)
    monkeypatch.setattr(voice, "_twilio_number", no_sms)
    yield


def wait_job(c, started, timeout=120):
    jid = started["job"]
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            assert j["status"] == "done", j
            return j["result"]
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def denied(actor_part=None):
    out = []
    for e in db.q("SELECT payload FROM event WHERE type='agent_action'"):
        p = e["payload"]
        if p["decision"] != "allow" and (actor_part is None or actor_part in p["actor"]):
            out.append(p)
    return out


def setup(c, headers=None, agent=False):
    """start -> add the data and the photo -> answer the questions. Returns the questions seen after the data was added."""
    h = headers or {}
    r = c.post("/api/setup2/start", json={"company": "Ασφάλειες Παπαδάκης", "website": SITE}, headers=h)
    assert r.status_code == 200, r.text
    if agent:
        with open(INS / "pelates.xlsx", "rb") as f:
            r = c.post("/api/setup2/add", files={"file": ("pelates.xlsx", f.read())}, headers=h)
    else:
        r = c.post("/api/setup2/add", json={"path": str(INS / "pelates.xlsx")}, headers=h)
    assert r.status_code == 200 and "error" not in r.json(), r.text
    for doc in ("timokatalogos.md", "oroi.md"):
        r = c.post("/api/setup2/add", json={"path": str(INS / doc)}, headers=h) if not agent else \
            c.post("/api/setup2/add", files={"file": (doc, (INS / doc).read_bytes())}, headers=h)
        assert r.status_code == 200, r.text
    # the list photo: the photo itself may be missing; FAKE reads list_photo.truth.json
    if not agent:
        r = c.post("/api/setup2/add", json={"path": str(INS / "paper" / "list_photo.jpg")}, headers=h)
        assert r.status_code == 200 and "error" not in r.json(), r.text
    qs = c.get("/api/setup2/questions", headers=h).json()
    return qs


def answer_all(c, qs, headers=None, consent="some"):
    h = headers or {}
    for _ in range(4):          # an answer can open a follow-up question (marks in the consent column)
        if not qs:
            break
        qs = _answer_batch(c, qs, h, consent)


def _answer_batch(c, qs, h, consent):
    for q in qs:
        opts = [o["id"] for o in q.get("options") or []]
        pick = consent if consent in opts else ("birth_year" if "birth_year" in opts else (opts[0] if opts else "ok"))
        r = c.post("/api/setup2/answer", json={"qid": q["id"], "answer": pick}, headers=h)
        assert r.status_code == 200 and "error" not in r.json(), r.text
    return c.get("/api/setup2/questions", headers=h).json()


def certify(c, headers=None):
    h = headers or {}
    r = c.post("/api/setup2/build", headers=h)
    assert r.status_code == 200, r.text
    res = wait_job(c, r.json())
    assert res.get("certified"), res
    cert = c.get("/api/setup2/review", headers=h).json()["certification"]
    assert cert["attacks"]["passed"] == cert["attacks"]["total"] > 0
    assert cert["setup_attacks"]["passed"] == cert["setup_attacks"]["total"] > 0
    assert cert["mutants"]["caught"] == cert["mutants"]["total"] > 0
    return res


def go_live_and_call(c):
    db.set_clock("2026-11-16T11:00")          # a Monday inside call hours
    # lanes are computed and every customer has exactly one
    lanes = c.get("/api/lanes").json()
    assert lanes["total"] == sum(lanes["counts"].values()) > 0
    assert lanes["counts"]["consent_request"] > 0
    sent = c.post("/api/consent/send", json={}).json()
    assert sent["sent"] > 0 and sent["channel"] == "web"
    link = sent["links"][0]
    tok = link["token"]
    page = c.get(f"/k/{tok}", headers=JSON)
    assert page.status_code == 200 and page.json()["token"] == tok and page.json()["text_version"]
    assert "<html" in c.get(f"/k/{tok}").text.lower()
    assert c.get("/k/nonexistenttoken", headers=JSON).status_code == 404
    assert c.get(f"/inbox/{tok}", headers=JSON).json()["items"][0]["kind"] == "consent_request"
    cust = db.get("customer", link["customer_id"])
    assert (cust.get("consent") or {}).get("source") != "tap"

    with pytest.raises(WebSocketDisconnect):          # a socket opened before the tap is refused
        with c.websocket_connect(f"/api/phone/ws?ct={tok}"):
            pass
    r = c.post(f"/k/{tok}", json={"choice": "call_now"})
    assert r.status_code == 200 and r.json()["status"] == "call_now" and r.json()["ws"].endswith(tok)
    assert db.get("customer", link["customer_id"])["consent"]["source"] == "tap"       # consent recorded by the tap
    assert db.q("SELECT 1 FROM event WHERE type='consent' AND payload LIKE '%given%'")
    with c.websocket_connect(f"/api/phone/ws?ct={tok}") as ws:
        assert ws.receive_json()["type"] == "ready"
        msg = ws.receive_json()
        while msg["type"] != "ring":
            msg = ws.receive_json()
        ws.send_json({"type": "answer"})
        seen = []
        while True:
            m = ws.receive_json()
            seen.append(m["type"])
            if m["type"] == "ended":
                break
        assert "caption" in seen
    job = next(j for j in server.jobs.values() if j["kind"] == "call")
    end = time.time() + 60
    while job["status"] == "running" and time.time() < end:
        time.sleep(0.1)
    assert job["status"] == "done" and job["result"]["outcome"] == "won", job
    assert db.get("call", job["result"]["call_id"])["via"] == "browser"
    conf = db.one("SELECT * FROM confirmation WHERE customer_id=?", (link["customer_id"],))
    assert conf and conf["channel"] == "stored"          # a web link, never an SMS
    page = c.get(f"/c/{conf['token']}")
    assert page.status_code == 200 and "<form" in page.text
    assert c.post(f"/c/{conf['token']}").status_code == 200
    assert db.get("customer", link["customer_id"])["status"] == "won"
    inbox = c.get(f"/api/inbox/{tok}").json()
    assert any(i["kind"] == "confirmation" for i in inbox["items"])


def test_demo_path_as_owner():
    with TestClient(server.app) as c:
        qs = setup(c)
        assert 1 <= len(qs) <= 3
        facts = {q["fact"] for q in qs}
        assert {"consent_basis", "identity_check"} <= facts
        # the planted instructions in the site were denied and logged, and none was applied
        d = denied()
        assert d and {"consent_needs_owner"} & {x["rule"] for x in d}
        case = c.get("/api/setup2/case").json()
        assert case["slots"]["max_discount_pct"]["value"] != 90
        # the OCR cells that need the owner's eye come with an image link
        rev = c.get("/api/setup2/review").json()
        assert rev["ocr_cells"] and all("image_url" in x for x in rev["ocr_cells"])
        answer_all(c, qs)
        assert c.get("/api/setup2/questions").json() == []
        certify(c)
        r = c.post("/api/setup2/request_turn_on")
        tok = r.json()["token"]
        assert c.get("/api/owner").json()["pending_turn_on"]["token"] == tok
        assert (db.company() or {}).get("status") != "live"
        r = c.post("/api/owner/turn_on", json={"token": tok})
        assert r.status_code == 200 and db.company()["status"] == "live", r.text
        assert c.post("/api/owner/turn_on", json={"token": tok}).status_code == 400      # single use
        go_live_and_call(c)


def test_demo_path_with_an_mcp_actor_for_setup():
    with TestClient(server.app) as c:
        qs = setup(c, AGENT, agent=True)
        assert qs and len(qs) <= 3
        # the agent answers; the author is the authenticated caller, not what the body says
        r = c.post("/api/setup2/answer", json={"qid": qs[0]["id"], "answer": {"option": "none", "by": "owner"}}, headers=AGENT)
        assert "cannot say who gave it" in r.text or "error" in r.json()
        answer_all(c, qs, AGENT, consent="none")      # an agent may only give the conservative consent answer
        cf = c.get("/api/setup2/case", headers=AGENT).json()
        assert any("claude-test" in str(a["by"]) for a in cf["attestations"])
        certify(c, AGENT)
        r = c.post("/api/setup2/request_turn_on", headers=AGENT)
        assert r.status_code == 200 and "token" not in r.json() and "path" not in r.json(), r.text
        tok = c.get("/api/owner").json()["pending_turn_on"]["token"]          # the owner sees it
        assert c.get("/api/owner", headers=AGENT).status_code == 403            # the agent does not
        for call in (lambda: c.post("/api/owner/turn_on", json={"token": tok}, headers=AGENT),
                     lambda: c.post(f"/t/{tok}", headers=AGENT),
                     lambda: c.post("/api/setup/approve", headers=AGENT)):
            assert call().status_code in (400, 403)
        assert db.company()["status"] != "live"
        # the page lists what the agent attested
        pg = c.get(f"/t/{tok}", headers=JSON).json()["pending_turn_on"]
        assert pg["attestations"] and all(a["by"].startswith("agent:claude-test") for a in pg["attestations"])
        assert c.post(f"/t/{tok}").status_code == 200             # the owner's tap (the link is the credential)
        assert db.company()["status"] == "live"
        go_live_and_call(c)


def test_forbidden_attempts_pause_an_agent_session_and_only_the_owner_resumes():
    with TestClient(server.app) as c:
        c.post("/api/setup2/start", json={"company": "X", "website": SITE}, headers=AGENT)
        assert c.post("/api/setup2/add", json={"path": str(INS / "oroi.md")}, headers=AGENT).status_code == 403   # no local paths for agents
        from prova import guard
        a = {"role": "mcp", "scope": "setup", "session": "mcp:test", "name": "test"}
        assert guard.check(a, "approve_and_go_live", {})["decision"] == "deny"
        assert limits.is_paused("mcp:test")
        assert c.post("/api/setup2/resume", headers=AGENT).status_code == 403
        assert c.post("/api/setup2/resume").status_code == 200


def test_dangerous_tools_are_not_available_to_an_mcp_actor():
    tools = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "approve_and_go_live" not in tools and "reset_everything" not in tools
    assert {"setup_start", "setup_add", "setup_answer", "setup_skip", "setup_status", "setup_review", "setup_build",
            "setup_request_turn_on", "find_due", "compliance_brief", "send_consent_requests", "get_brief"} <= tools
    with TestClient(server.app) as c:
        for path in ("/api/reset", "/api/setup/approve", "/api/setup/live"):
            assert c.post(path, headers=AGENT).status_code == 403
        assert c.post("/api/setup2/request_turn_on", headers=AGENT).status_code in (200, 400)
