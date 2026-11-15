import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PROVA_DB"] = os.path.join(tempfile.mkdtemp(), "law.db")
os.environ["PROVA_FAKE"] = "1"

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from prova import db, law, policy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (ROOT / "docs" / "law_sources.md").read_text(encoding="utf-8")


def all_items():
    return [i for j in law.JURISDICTIONS.values() for i in j.get("laws", [])]


@pytest.mark.parametrize("item", all_items(), ids=lambda i: i["id"])
def test_every_quote_is_in_law_sources(item):
    assert item["quote"] and item["quote"] in SOURCES
    assert f"`{item['source_id']}`" in SOURCES


def test_s_only_sources_are_unverified():
    by = {i["id"]: i["verified"] for i in all_items()}
    for k in ("gr-3471-11", "gr-4583-33-7", "eu-gdpr-21", "eu-gdpr-13", "eu-gdpr-22"):   # status S in docs/law_sources.md
        assert by[k] is False, k
    for k in ("eu-eprivacy-13", "eu-idd-20", "eu-idd-23", "eu-ai-act-50"):               # A / P quotes
        assert by[k] is True, k


def test_gr_pack_human_calls_disabled_and_merged():
    p = law.pack("GR")
    assert p["lanes"]["human_calls"] == "disabled_until_source"
    ids = {i["id"] for i in p["laws"]}
    assert {"eu-eprivacy-13", "gr-3471-11"} <= ids
    assert p["country"] == "GR" and "el" in p["opening"]


def test_unknown_country_gets_strictest():
    assert law.pack("XX")["country"] == "GR" and law.pack(None)["lanes"]["human_calls"] == "disabled_until_source"
    assert law.pack("eu")["country"] == "EU"


def test_floor_must_say_right_to_object_at_opening():
    ms = law.pack("GR")["floor_must_say"]
    assert any(m["stage"] == "opening" and m["law_item"] == "eu-gdpr-21" for m in ms)
    db.reset()
    eff = policy.effective()
    assert any(m["stage"] == "opening" and m.get("floor") for m in eff["must_say"])
    assert eff["call_hours"]["from"]


def test_unverified_items_labelled():
    labels = {i["id"]: i["label"] for i in law.pack("GR")["laws"]}
    assert labels["gr-3471-11"].startswith("unverified") and labels["eu-eprivacy-13"] == "verified"
    assert "gr-3471-11" not in law.verified_ids("GR")


def test_insurance_pack_shape():
    d = json.loads((ROOT / "packs" / "insurance.json").read_text(encoding="utf-8"))
    assert {"goals_hint", "slot_hints", "personas", "attack_seeds", "proof_fields", "glossary"} <= set(d)
    from prova import evals
    assert all(hasattr(evals, a["checker"]) for a in d["attack_seeds"])
    known = {i["id"] for i in all_items()}
    assert set(d["law_items"]) <= known


def test_new_slots_and_tables():
    from prova import template
    db.reset()
    keys = {"country", "consent_basis", "call_hours", "must_say", "blocked_segments", "opening_lines", "human_calls_allowed"}
    assert keys <= set(template.SLOT_KEYS)
    req = {s["key"]: s["required"] for s in template.POLICY_SLOTS}
    assert req["country"] and not req["call_hours"] and not req["consent_basis"] and not req["human_calls_allowed"]
    assert {"language", "opt_out_register"} <= set(template.CUSTOMER_FIELDS)
    assert db.one("SELECT COUNT(*) n FROM consent_request")["n"] == 0
    for t in ("consent", "question", "agent_action"):
        db.emit(t, payload={"x": 1})
    assert db.verify_chain()["ok"]
