"""Text model helper. JSON in, JSON out, spend recorded per call.

  ask_json(prompt, system="", fake=None, call_id=None, tier="main")      blocking
  await aask_json(...)                                                    same, off the event loop

FAKE mode (env PROVA_FAKE=1): no network, no spend. ask_json returns `fake` (a dict, or a function that
returns one). Every caller MUST pass a sensible `fake` so the whole product runs offline for UI work and tests.

Tiers: "main" for reasoning (slot filling, review, twins), "fast" for the live sidecar (classify, rank).
Models can be overridden with PROVA_LLM_MODEL / PROVA_FAST_MODEL.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = os.environ.get("PROVA_FAKE") == "1"

# Concrete model names tried in order; a NOT_FOUND drops the head of the list.
MODELS = {
    "main": [m for m in (os.environ.get("PROVA_LLM_MODEL"), "gemini-3.5-flash", "gemini-3.8-flash") if m],
    "fast": [m for m in (os.environ.get("PROVA_FAST_MODEL"), "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.8-flash") if m],
}
# Rough EUR per 1M tokens (input, output). Only used for the cost meter; adjust if pricing differs.
PRICE = {"main": (0.30, 2.50), "fast": (0.10, 0.40)}
VOICE_EUR_PER_MIN = 0.045     # live voice session, both directions, rough
MAX_CALLS = 3000
_calls = 0
_client = None


def load_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing: put it in .env")
    return key


def call_count() -> int:
    return _calls


def _parse(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def _fake(fake):
    if fake is None:
        raise RuntimeError("PROVA_FAKE=1 but this ask_json call has no `fake` value")
    return fake() if callable(fake) else fake


def ask_json(prompt: str, system: str = "", fake=None, call_id=None, tier: str = "main", temperature: float = 0.4):
    """One JSON answer. Retries once on error or invalid JSON. Records cost (and adds it to the call if call_id)."""
    global _calls, _client
    if FAKE:
        return _fake(fake)
    from google import genai
    from google.genai import types

    from prova import db

    if _client is None:
        _client = genai.Client(api_key=load_key())
    cfg = types.GenerateContentConfig(response_mime_type="application/json", system_instruction=system or None,
                                      temperature=temperature)
    models = MODELS[tier]
    last = None
    for _ in range(3):
        if _calls >= MAX_CALLS:
            raise RuntimeError(f"llm call cap reached ({MAX_CALLS} per process)")
        _calls += 1
        try:
            r = _client.models.generate_content(model=models[0], contents=prompt, config=cfg)
            u = getattr(r, "usage_metadata", None)
            tin = getattr(u, "prompt_token_count", 0) or 0
            tout = (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
            pin, pout = PRICE[tier]
            try:
                db.add_cost("llm", (tin * pin + tout * pout) / 1e6, call_id=call_id, model=models[0], units={"in": tin, "out": tout})
            except Exception:  # noqa: BLE001  cost meter must never break a call
                pass
            return _parse(r.text)
        except Exception as e:  # noqa: BLE001
            last = e
            if "NOT_FOUND" in str(e) and len(models) > 1:
                models.pop(0)  # model missing for this key: use the next one
            else:
                time.sleep(0.4)
    raise RuntimeError(f"ask_json failed: {type(last).__name__}: {str(last)[:200]}")


async def aask_json(prompt: str, system: str = "", fake=None, call_id=None, tier: str = "main", temperature: float = 0.4):
    """ask_json without blocking the event loop. Use this inside calls, sims and the sidecar."""
    if FAKE:
        return _fake(fake)
    return await asyncio.to_thread(ask_json, prompt, system, fake, call_id, tier, temperature)


def ask_json_media(prompt: str, files: list, system: str = "", fake=None, call_id=None, tier: str = "main",
                   temperature: float = 0.1):
    """ask_json with images / PDFs: files = [(bytes, mime_type), ...]. Same FAKE and cost behaviour."""
    global _calls, _client
    if FAKE:
        return _fake(fake)
    from google import genai
    from google.genai import types

    from prova import db

    if _client is None:
        _client = genai.Client(api_key=load_key())
    parts = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in files] + [prompt]
    cfg = types.GenerateContentConfig(response_mime_type="application/json", system_instruction=system or None,
                                      temperature=temperature)
    models = MODELS[tier]
    last = None
    for _ in range(3):
        if _calls >= MAX_CALLS:
            raise RuntimeError(f"llm call cap reached ({MAX_CALLS} per process)")
        _calls += 1
        try:
            r = _client.models.generate_content(model=models[0], contents=parts, config=cfg)
            u = getattr(r, "usage_metadata", None)
            tin = getattr(u, "prompt_token_count", 0) or 0
            tout = (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
            pin, pout = PRICE[tier]
            try:
                db.add_cost("llm", (tin * pin + tout * pout) / 1e6, call_id=call_id, model=models[0], units={"in": tin, "out": tout})
            except Exception:  # noqa: BLE001
                pass
            return _parse(r.text)
        except Exception as e:  # noqa: BLE001
            last = e
            if "NOT_FOUND" in str(e) and len(models) > 1:
                models.pop(0)
            else:
                time.sleep(0.4)
    raise RuntimeError(f"ask_json_media failed: {type(last).__name__}: {str(last)[:200]}")


async def aask_json_media(prompt: str, files: list, system: str = "", fake=None, call_id=None, tier: str = "main",
                          temperature: float = 0.1):
    if FAKE:
        return _fake(fake)
    return await asyncio.to_thread(ask_json_media, prompt, files, system, fake, call_id, tier, temperature)


def voice_cost(seconds: float) -> float:
    return round(seconds / 60 * VOICE_EUR_PER_MIN, 4)
