"""Renewal Desk call runner: Gemini Live caller agent <-> simulated customer (turn-based bridge).

  uv run --with google-genai --with numpy python -m desk.call <customer_id>

Both sides are Gemini Live sessions with manual activity detection. One side speaks a full turn,
its audio is resampled 24k -> 16k and fed to the other side as activity_start/audio/activity_end.
No real-time pacing, so a call runs faster than real time.
"""
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

from desk import db
from desk.prompts import caller_prompt, customer_sim_prompt

ROOT = Path(__file__).resolve().parent.parent
MODEL = "gemini-3.1-flash-live-preview"
CAP_S, MAX_EXCHANGES = 150, 10
CALLER_VOICE, CUSTOMER_VOICES = "Kore", ["Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus"]

TOOLS = [{"function_declarations": [
    {"name": "get_offers", "description": "List the offers you may sell."},
    {"name": "quote_price", "description": "Check a monthly price BEFORE you say it or agree to it: a price the customer asked for, "
     "or a discounted price you want to offer. Returns whether it is allowed and the exact discount_pct to pass to renew_contract.",
     "parameters": {"type": "OBJECT", "properties": {"offer_id": {"type": "STRING"}, "monthly_price": {"type": "NUMBER"}},
                    "required": ["offer_id", "monthly_price"]}},
    {"name": "renew_contract", "description": "Record that the customer agreed to a renewal.",
     "parameters": {"type": "OBJECT", "properties": {"offer_id": {"type": "STRING"}, "discount_pct": {"type": "NUMBER"}},
                    "required": ["offer_id", "discount_pct"]}},
    {"name": "schedule_callback", "description": "Schedule a call back to the customer.",
     "parameters": {"type": "OBJECT", "properties": {"when": {"type": "STRING"}, "note": {"type": "STRING"}},
                    "required": ["when", "note"]}},
    {"name": "transfer_to_human", "description": "Hand the customer to a human agent now.",
     "parameters": {"type": "OBJECT", "properties": {"reason": {"type": "STRING"}}, "required": ["reason"]}},
    {"name": "mark_do_not_call", "description": "Customer asked not to be called again."},
    {"name": "end_call", "description": "Finish the call. Always the last action.",
     "parameters": {"type": "OBJECT", "properties": {
         "outcome": {"type": "STRING", "description": "renewed, upgraded, callback, thinking, human, declined or do_not_call"},
         "note": {"type": "STRING"}}, "required": ["outcome", "note"]}},
]}]


def load_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")
    return key


def resample_24_to_16(pcm: bytes) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) < 2:
        return pcm
    n = int(len(x) * 2 / 3)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def save_wav(path, pcm, rate=24000):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def live_config(prompt, voice, tools=None):
    cfg = {
        "response_modalities": ["AUDIO"],
        "system_instruction": prompt,
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice}}},
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
    }
    if tools:
        cfg["tools"] = tools
    return cfg


class Call:
    def __init__(self, call_id):
        self.id = call_id
        self.call = db.get("calls", call_id)
        self.customer = db.get("customers", self.call["customer_id"])
        self.brief = db.get("briefs", self.call["brief_id"]) if self.call["brief_id"] else None
        self.offers = db.q("SELECT * FROM offers")
        self.offer_by_id = {o["id"]: o for o in self.offers}
        self.fired = {}  # tool name -> args
        self.end = None  # end_call args
        self.detail = {}
        self.t0 = time.time()
        self.wav = bytearray()
        self.exchanges = 0
        self.capped = False
        self.rejected = None  # last refused renew_contract, if any

    def left(self):
        return CAP_S - (time.time() - self.t0)

    # ----- tools (caller side) -----
    def run_tool(self, name, args):
        args = dict(args or {})
        res = self._tool(name, args)
        ok = not (isinstance(res, dict) and "error" in res)
        if ok and name not in ("get_offers", "quote_price"):
            self.fired[name] = args
        db.add_transcript(self.id, "tool", f"{name}({args}) -> {res if not ok else 'ok'}")
        db.emit("tool", call_id=self.id, customer_id=self.customer["id"], payload={"name": name, "args": args, "result": res})
        return res

    def _tool(self, name, a):
        ceiling = (self.brief or {}).get("max_discount_pct") or 0
        if name == "get_offers":
            return {"offers": [{k: o[k] for k in ("id", "name", "monthly_price", "data_gb", "minutes", "term_months", "perks")}
                               for o in self.offers]}
        if name == "quote_price":
            o = self.offer_by_id.get(a.get("offer_id"))
            if not o:
                return {"error": f"unknown offer_id. Valid: {list(self.offer_by_id)}"}
            lim = min(ceiling, o["max_discount_pct"])
            lst, floor = o["monthly_price"], round(o["monthly_price"] * (1 - lim / 100), 2)
            p = round(float(a.get("monthly_price") or 0), 2)
            if p >= lst:
                return {"allowed": True, "monthly_price": lst, "discount_pct": 0, "say": f"That is the list price, {lst:.2f} EUR/month."}
            if p >= floor - 0.005:
                d = round((1 - p / lst) * 100, 2)
                return {"allowed": True, "monthly_price": p, "discount_pct": d,
                        "say": f"Allowed. If the customer asked for {p:.2f}, agree to exactly that and never offer lower."}
            return {"allowed": False, "lowest_price": floor, "discount_pct": lim,
                    "say": f"{p:.2f} is below the limit. The best possible is {floor:.2f} EUR/month (discount_pct {lim:g}); offer that as your final price."}
        if name == "renew_contract":
            o = self.offer_by_id.get(a.get("offer_id"))
            if not o:
                return {"error": f"unknown offer_id. Valid: {list(self.offer_by_id)}"}
            d = float(a.get("discount_pct") or 0)
            lim = min(ceiling, o["max_discount_pct"])
            if d > lim + 1e-6 or d < 0:
                self.rejected = f"renew_contract({o['id']}, {d:g}%) rejected: ceiling is {lim:g}%"
                floor = round(o["monthly_price"] * (1 - lim / 100), 2)
                return {"error": f"discount {d}% not allowed. The ceiling for this customer and offer is {lim}%. NOTHING is confirmed yet. "
                                 f"Tell the customer the lowest possible price is {floor} EUR/month, ask if that is ok, then retry with {lim}% or less."}
            self.detail.update(offer_id=o["id"], discount_pct=d)
            return {"status": "renewed", "offer": o["name"], "monthly_price": round(o["monthly_price"] * (1 - d / 100), 2),
                    "next": "thank them, then call end_call"}
        if name == "schedule_callback":
            self.detail.update(callback_at=a.get("when"), note=a.get("note"))
            return {"status": "scheduled", "when": a.get("when"), "next": "say a brief goodbye, then call end_call"}
        if name == "transfer_to_human":
            self.detail.update(reason=a.get("reason"))
            return {"status": "transferred", "next": "say a brief goodbye, then call end_call(outcome=human)"}
        if name == "mark_do_not_call":
            return {"status": "marked", "next": "apologise briefly, then call end_call(outcome=do_not_call)"}
        if name == "end_call":
            self.end = a
            return {"status": "call will end after your goodbye"}
        return {"error": f"unknown tool {name}"}

    def outcome(self):
        f = self.fired
        if "renew_contract" in f:
            o = self.offer_by_id[f["renew_contract"]["offer_id"]]
            return "upgraded" if o["monthly_price"] > (self.customer["monthly_price"] or 0) else "renewed"
        if "mark_do_not_call" in f:
            return "do_not_call"
        if "transfer_to_human" in f:
            return "human"
        if "schedule_callback" in f:
            return "callback"
        e = (self.end or {}).get("outcome")
        if self.rejected and (e in ("renewed", "upgraded", "human") or not e):
            # the customer agreed to terms the system refused: a person must sort it out, never leave it hanging
            self.detail.setdefault("reason", f"sale agreed on the call but not booked ({self.rejected})")
            return "human"
        # outcomes that only protect the customer are honoured even if the agent skipped the matching tool
        if e in ("human", "do_not_call", "callback"):
            if e == "human":
                self.detail.setdefault("reason", (self.end or {}).get("note") or "agent ended the call as a handoff")
            return e
        if e in db.OUTCOMES and e not in ("renewed", "upgraded", "failed", "no_answer"):
            return e
        return "thinking"


def join_text(prev: str, frag: str) -> str:
    """Join transcript fragments; add a space at sentence boundaries that lack one."""
    if prev and frag and not prev[-1].isspace() and not frag[0].isspace() and prev[-1] in ".?!,:;" and frag[0].isalnum():
        return prev + " " + frag
    return prev + frag


async def collect_turn(session, call, role, tools=False, timeout=60):
    """Read one model turn. Logs speech and tool lines in the order they happened.
    Returns (pcm, text). Executes tool calls on the caller session. Bounded by the call cap."""
    pcm, text = bytearray(), ""
    logged = [""]

    def flush():
        nonlocal text
        from desk.phone import strip_leak  # drop a function call the model spoke instead of calling
        t = strip_leak(text).strip()
        if t:
            db.add_transcript(call.id, role, t)
            logged[0] = (logged[0] + " " + t).strip()
        text = ""

    async def loop():
        nonlocal text
        async for msg in session.receive():
            if msg.tool_call and tools:
                from google.genai import types
                flush()  # speech so far comes before the tool line
                resps = []
                for fc in msg.tool_call.function_calls:
                    resps.append(types.FunctionResponse(id=fc.id, name=fc.name, response=call.run_tool(fc.name, fc.args)))
                await session.send_tool_response(function_responses=resps)
                continue
            sc = msg.server_content
            if not sc:
                continue
            if sc.output_transcription and sc.output_transcription.text:
                text = join_text(text, sc.output_transcription.text)
            if sc.model_turn:
                for p in sc.model_turn.parts or []:
                    if p.inline_data and p.inline_data.data:
                        pcm.extend(p.inline_data.data)
            if sc.turn_complete:
                return

    try:
        await asyncio.wait_for(loop(), timeout=max(3, min(timeout, call.left() + 5)))
    finally:
        flush()
    return bytes(pcm), logged[0]


async def feed_audio(session, pcm24):
    from google.genai import types
    pcm = resample_24_to_16(pcm24)
    await session.send_realtime_input(activity_start=types.ActivityStart())
    step = 3200  # 100 ms
    for i in range(0, len(pcm), step):
        await session.send_realtime_input(audio=types.Blob(data=pcm[i:i + step], mime_type="audio/pcm;rate=16000"))
    await session.send_realtime_input(activity_end=types.ActivityEnd())


def history_for(customer_id, call_id):
    """Earlier ended calls; adds a fallback summary (note + what the customer said) when no review exists."""
    out = []
    for h in db.q("SELECT * FROM calls WHERE customer_id=? AND id<>? AND status='ended' ORDER BY id", (customer_id, call_id)):
        if not h.get("summary"):
            det = h.get("outcome_detail") or {}
            said = " ".join(l["text"] for l in (h.get("transcript") or []) if l["role"] == "customer")[:400]
            h["summary"] = f"Agent note: {det.get('note') or 'none'}. Customer said: {said}"
        out.append(h)
    return out


async def run_sim(c: Call):
    from google import genai
    client = genai.Client(api_key=load_key(), http_options={"api_version": "v1beta"})
    caller_p = caller_prompt(db.public_customer(c.customer), c.brief, c.offers, history_for(c.customer["id"], c.id))
    cust_p = customer_sim_prompt(c.customer)
    persona = (c.customer.get("persona") or "").strip()
    if persona and persona in caller_p:
        raise RuntimeError("persona leaked into caller prompt")
    voice = CUSTOMER_VOICES[c.customer["id"] % len(CUSTOMER_VOICES)]
    gap = bytes(int(24000 * 0.35) * 2)
    wav = c.wav
    async with client.aio.live.connect(model=MODEL, config=live_config(caller_p, CALLER_VOICE, TOOLS)) as caller, \
            client.aio.live.connect(model=MODEL, config=live_config(cust_p, voice)) as cust:
        await cust.send_realtime_input(text="(your phone rings, you pick up)")
        for ex in range(MAX_EXCHANGES):
            if c.left() <= 0:
                break
            c.exchanges = ex + 1
            pcm, _ = await collect_turn(cust, c, "customer")
            wav += pcm + gap
            if not pcm:
                break
            await feed_audio(caller, pcm)
            pcm, _ = await collect_turn(caller, c, "agent", tools=True)
            wav += pcm + gap
            terminal = {"renew_contract", "schedule_callback", "transfer_to_human", "mark_do_not_call"} & set(c.fired)
            if terminal and not c.end:
                # agent forgot end_call: nudge it once
                await caller.send_realtime_input(text="(system: the matter is settled. If you have not said goodbye, say it in one short sentence, then call end_call now.)")
                try:
                    p2, _ = await collect_turn(caller, c, "agent", tools=True, timeout=20)
                    wav += p2 + gap
                except asyncio.TimeoutError:
                    pass
            if c.end:
                if not pcm:  # end_call came without speech: let a final goodbye play
                    try:
                        p2, _ = await collect_turn(caller, c, "agent", tools=True, timeout=15)
                        wav += p2 + gap
                    except asyncio.TimeoutError:
                        pass
                break
            if terminal or not pcm:
                break
            await feed_audio(cust, pcm)


def default_brief(customer_id, campaign_id=None):
    c = db.get("customers", customer_id)
    offers = [o for o in db.q("SELECT * FROM offers") if o["id"] in ("basic", "smart", "max", "family")]
    offers.sort(key=lambda o: o["data_gb"])
    rec = next((o for o in offers if o["data_gb"] >= (c["data_gb_used_avg"] or 0) * 1.15), offers[-1])
    if c["plan_id"] == rec["id"] or (c["data_gb_used_avg"] or 0) < 0.6 * next((o["data_gb"] for o in offers if o["id"] == c["plan_id"]), 1e9):
        rec = next((o for o in offers if o["id"] == c["plan_id"]), rec)
    return db.insert(
        "briefs", customer_id=customer_id, campaign_id=campaign_id, win_chance=0.5,
        recommended_offer_id=rec["id"], fallback_offer_id=c["plan_id"], max_discount_pct=min(15, rec["max_discount_pct"]),
        persistence=2, opening_hook=f"Your contract ends on {c['contract_end']} and you have been with us {c['tenure_months']} months.",
        likely_objection="price", talking_points=[c["notes"] or "Loyal customer"], author="auto", created_at=db.now_iso())


async def run_call(call_id: int, mode: str = "sim") -> dict:
    t0 = time.time()
    c = Call(call_id)
    cid = c.customer["id"]
    detail, outcome, status, audio_path = {}, "failed", "failed", None
    try:
        persona = (c.customer.get("persona") or "").lower()
        db.update("calls", call_id, status="live", started_at=db.now_iso())
        db.emit("call_started", call_id=call_id, customer_id=cid, payload={"mode": mode})
        if "does not pick up" in persona and (c.call.get("attempt") or 1) == 1:
            outcome, status, detail = "no_answer", "ended", {"note": "No answer"}
        else:
            if not c.brief:
                bid = default_brief(cid)
                db.update("calls", call_id, brief_id=bid)
                c.brief = db.get("briefs", bid)
            if mode != "sim":
                raise NotImplementedError("mic mode not implemented")
            try:  # hard cap: bounds connect, feed, receive and tool replies, not just turns
                await asyncio.wait_for(run_sim(c), timeout=CAP_S + 8)
            except asyncio.TimeoutError:
                c.capped = True
            wav = bytes(c.wav)
            if wav:
                audio_path = str(ROOT / "out" / "calls" / f"call_{call_id}.wav")
                save_wav(audio_path, wav)
            outcome = c.outcome()
            status = "ended"
            detail = {"offer_id": None, "discount_pct": None, "callback_at": None, "reason": None, "note": None}
            detail.update(c.detail)
            detail["note"] = (c.end or {}).get("note") or c.detail.get("note") or ("ended without end_call (time cap)" if c.capped else "ended without end_call" if not c.end else None)
    except Exception as e:  # noqa: BLE001
        outcome, status = "failed", "failed"
        detail = {"note": f"{type(e).__name__}: {e}"}
    dur = round(time.time() - t0, 1)
    db.update("calls", call_id, status=status, outcome=outcome, outcome_detail=detail, ended_at=db.now_iso(),
              duration_s=dur, audio_path=audio_path)
    db.emit("call_ended", call_id=call_id, customer_id=cid, payload={"outcome": outcome, "duration_s": dur})
    detail["exchanges"] = c.exchanges
    db.update("calls", call_id, outcome_detail=detail)
    return {"call_id": call_id, "exchanges": c.exchanges, "end_call": bool(c.end), "outcome": outcome, "outcome_detail": detail, "duration_s": dur}


def main():
    db.init()
    cid = int(sys.argv[1])
    brief = db.one("SELECT * FROM briefs WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cid,))
    bid = brief["id"] if brief else default_brief(cid)
    attempt = (db.one("SELECT COUNT(*) n FROM calls WHERE customer_id=?", (cid,)) or {"n": 0})["n"] + 1
    call_id = db.insert("calls", customer_id=cid, brief_id=bid, kind="followup" if attempt > 1 else "first", attempt=attempt, status="queued")
    res = asyncio.run(run_call(call_id, "sim"))
    for l in db.get("calls", call_id)["transcript"]:
        print(f"{l['role'].upper():9}: {l['text']}")
    print(res)


if __name__ == "__main__":
    main()
