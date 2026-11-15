import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp()
os.environ["PROVA_DB"] = os.path.join(_tmp, "goals.db")
os.environ["PROVA_OUT"] = os.path.join(_tmp, "out")
os.environ["PROVA_FAKE"] = "1"

import pytest  # noqa: E402

from prova import compliance, db, flow, goals, onboard, template  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(autouse=True)
def fresh():
    db.reset()


def mk(name, phone, **fields):
    return db.insert("customer", name=name, phone=phone, fields={"name": name, "phone": phone, **fields},
                     consent={"automated_calls": True, "source": "t", "at": None}, status="active", flags=[])


def days(n):
    return (db.now() + __import__("datetime").timedelta(days=n)).date().isoformat()


def test_suggest_goal_from_insurance_file_is_renew():
    onboard.ingest(str(SAMPLES / "insurance" / "pelates.xlsx"))
    s = goals.suggest_goal()
    assert s["goal_type"] == "renew" and s["counts"]["renew"] > 10 and s["eur_at_stake"] > 0
    assert "45" in s["goal_text"] and onboard.goal_from_text(s["goal_text"]) == "renew"


def test_suggest_goal_picks_the_template_the_columns_fit():
    mk("A", "+306900000001", last_active_date=days(-200), price=30, period="month")
    mk("B", "+306900000002", last_active_date=days(-120), price=30, period="month")
    s = goals.suggest_goal()
    assert s["goal_type"] == "win_back" and onboard.goal_from_text(s["goal_text"]) == "win_back"
    db.reset()
    mk("C", "+306900000003", amount_due=120.5, due_date=days(-5))
    s = goals.suggest_goal()
    assert s["goal_type"] == "collect" and s["eur_at_stake"] == 120.5 and onboard.goal_from_text(s["goal_text"]) == "collect"
    db.reset()
    mk("D", "+306900000004", due_date=days(5))
    assert goals.suggest_goal()["goal_type"] == "remind"


def test_suggest_goal_without_usable_columns_asks():
    mk("E", "+306900000005")
    s = goals.suggest_goal()
    assert s["goal_type"] is None and s["goal_text"] == ""


def test_radar_lists_due_customers_with_lane_and_euros(monkeypatch):
    a = mk("In window", "+306900000011", expiry_date=days(10), price=200, period="year")
    b = mk("Monthly", "+306900000012", expiry_date=days(30), price=20, period="month")
    mk("Too far", "+306900000013", expiry_date=days(80), price=999)
    mk("Past", "+306900000014", expiry_date=days(-3), price=999)
    monkeypatch.setattr(compliance, "lane", lambda c: {"lane": "ai_call" if c["id"] == a else "consent_request", "rule": "t", "reason": "test", "next": "call"})
    r = goals.radar("renew", 45)
    assert [x["customer_id"] for x in r["due"]] == [a, b]
    assert r["eur_at_stake"] == 440 and r["due"][1]["eur"] == 240        # monthly price x12
    assert r["lanes"]["ai_call"] == {"customers": 1, "eur": 200.0} and r["lanes"]["consent_request"]["customers"] == 1
    assert r["due"][0]["lane"] == "ai_call" and r["due"][0]["lane_rule"] == "t" and r["due"][0]["days"] == 10
    assert goals.radar(None)["goal"] == "renew"                          # no goal: the suggestion


def test_radar_with_no_goal_available():
    mk("E", "+306900000005")
    assert goals.radar()["due"] == [] and goals.radar()["error"]


def test_brief_headline_and_open_questions(monkeypatch):
    a = mk("A", "+306900000021", expiry_date=days(5), price=100)
    mk("B", "+306900000022", expiry_date=days(6), price=50)
    monkeypatch.setattr(compliance, "lane", lambda c: {"lane": "ai_call" if c["id"] == a else "hold", "rule": "t", "reason": "r", "next": "none"})
    b = goals.brief()
    assert b["goal"] == "renew" and "2 customers are due" in b["headline"] and "100" in b["headline"]
    assert b["radar"]["lanes"]["ai_call"]["customers"] == 1 and b["top"][0]["customer_id"] == a


def test_rank_customers_skips_other_lanes_with_the_lane_as_reason(monkeypatch):
    ok = mk("Ok", "+306900000031", expiry_date=days(3))
    ask = mk("Ask", "+306900000032", expiry_date=days(3))
    db.update("company", 1, goal_type="renew")
    db.set_slot("allowed_purposes", ["renew"], "t", "confirmed")
    db.set_slot("max_attempts", 3, "t", "confirmed")
    db.set_slot("min_gap_hours", 1, "t", "confirmed")
    lanes = {ok: {"lane": "ai_call", "rule": "consent_file", "reason": "ok", "next": "call"},
             ask: {"lane": "consent_request", "rule": "consent_unclear", "reason": "x mark", "next": "send_request"}}
    monkeypatch.setattr(compliance, "lane", lambda c: lanes[c["id"]])
    r = flow.rank_customers("sim")
    by = {x["customer_id"]: x for x in r}
    assert by[ok]["callable"] and not by[ask]["callable"]
    assert by[ask]["reason"].startswith("not callable: lane consent_request") and by[ask]["rule"] == "consent_unclear"
    assert db.get("customer", ask)["rank_reason"].startswith("not callable: lane consent_request")


def test_rank_customers_ignores_a_stub_lane(monkeypatch):
    mk("Ok", "+306900000041", expiry_date=days(3))
    db.update("company", 1, goal_type="renew")
    db.set_slot("allowed_purposes", ["renew"], "t", "confirmed")
    monkeypatch.setattr(compliance, "lane", lambda c: {"lane": "hold", "rule": "stub", "reason": "not implemented", "next": "none"})
    r = flow.rank_customers("sim")
    assert r[0]["callable"], r


def test_run_all_without_goal_uses_the_suggestion():
    r = asyncio.run(flow.run_all("samples/insurance/pelates.xlsx", ["samples/insurance/timokatalogos.md", "samples/insurance/oroi.md"], None,
                                 auto_approve=False))
    assert r["goal"]["goal_type"] == "renew"
    assert db.company()["goal_type"] == "renew" and "45" in db.company()["goal_text"]
