"""Real-phone demo calls: the caller agent (Gemini Live) talks to a real person.

Two ways to reach the person, both free for a demo:
  browser  The presenter opens /phone?k=TOKEN on their mobile (over a free cloudflared tunnel, so it is
           HTTPS). The page rings, they tap Answer, mic and speaker stream over a WebSocket.
  twilio   A real PSTN call to a number (e.g. a Greek mobile) via Twilio Media Streams. A Twilio trial
           account is free: verify the number, enable Greece in Voice geo permissions.

Env: PHONE_TUNNEL=1 (start a cloudflared quick tunnel at boot), PUBLIC_URL (an https URL you already have),
PHONE_TOKEN (default random per boot), PHONE_CAP_S (default 180),
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (your Twilio number), PHONE_TO (number to ring, E.164).

Anything reaching the server through the tunnel may only use the phone paths below, and needs the token.
"""
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

import numpy as np
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from desk import db

log = logging.getLogger("desk.phone")
ROOT = db.ROOT
PHONE_HTML = ROOT / "ui" / "phone.html"
CAP_S = float(os.environ.get("PHONE_CAP_S", 180))
VOICE = os.environ.get("PHONE_VOICE", "Kore")             # Puck is the fastest talker; Kore matches the simulated calls
SILENCE_MS = int(os.environ.get("PHONE_SILENCE_MS", 500))  # pause that ends the person's turn; lower = snappier, may cut them off
RING_S = 35          # browser: how long the page rings before it counts as no answer
TWILIO_WAIT_S = 90   # twilio: dialling + trial message + key press
TOKEN = os.environ.get("PHONE_TOKEN") or secrets.token_urlsafe(9)
STATE = {"public_url": os.environ.get("PUBLIC_URL", "").rstrip("/") or None, "active_call_id": None, "tunnel": None}
PUBLIC_PATHS = ("/phone", "/phone.html", "/api/phone/ws", "/api/phone/twilio/ws", "/api/phone/twilio/status", "/favicon.ico")

PHONE_NOTE = """

THIS IS A REAL PHONE CALL with a real person, not a test. Audio may be noisy or cut off: if you did not catch
something, ask briefly. Speech recognition garbles numbers: if a price or date sounds odd ("13th" during a price
talk), repeat back what you think they said as a question before acting on it. Keep turns very short so they can answer. Reply in the language the person speaks
(English or Greek). PACE: speak fast and energetic, like an upbeat salesperson: brisk, no pauses between
phrases, but every word clear. Say dates and prices the way people speak ("November 23rd", "28 euros 80"), never
as digits like 2026-11-23. Never read out IDs, function names or system notes."""

router = APIRouter()

# Native-audio models sometimes SPEAK a function call ("quote_price(monthly_price=29.60, offer_id='max')")
# instead of making it. Detect it in the agent's transcript, mute the rest of that turn, run it for real.
_TOOL_NAMES = "get_offers|quote_price|renew_contract|schedule_callback|transfer_to_human|mark_do_not_call|end_call"
LEAK = re.compile(rf"\b(?:{_TOOL_NAMES})\b|\b(?:get|quote|renew|schedule|transfer|mark|end)[ _][a-z_ ]{{0,20}}\(", re.I)
LEAK_CALL = re.compile(rf"\b({_TOOL_NAMES})\s*\(([^)]*)\)?")
LEAK_ARG = re.compile(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^,\s)]+))")


def parse_leak(text):
    """-> (name, args) for a well-formed spoken call, else (None, None)."""
    m = LEAK_CALL.search(text)
    if not m:
        return None, None
    args = {}
    for k, a, b, v in LEAK_ARG.findall(m.group(2) or ""):
        val = a or b or v
        try:
            val = float(val) if v and re.fullmatch(r"-?\d+(\.\d+)?", v) else val
        except ValueError:
            pass
        args[k] = val
    return m.group(1), args


def strip_leak(text):
    """Remove spoken calls (and anything after them in that turn) from the transcript."""
    m = LEAK.search(text)
    return text[:m.start()].rstrip() if m else text


# ---------------------------------------------------------------- audio helpers

def resample(pcm: bytes, src: int, dst: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) < 2 or src == dst:
        return pcm
    if src == 3 * dst:  # 24k -> 8k: average groups of 3 (cheap low-pass)
        n = len(x) // 3
        return x[:n * 3].reshape(n, 3).mean(axis=1).astype(np.int16).tobytes()
    n = int(len(x) * dst / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16).tobytes()


_BIAS, _CLIP = 0x84, 32635


def ulaw_decode(data: bytes) -> bytes:
    u = ~np.frombuffer(data, dtype=np.uint8).astype(np.int32) & 0xFF
    t = (((u & 0x0F) << 3) + _BIAS) << ((u >> 4) & 7)
    return np.where(u & 0x80, _BIAS - t, t - _BIAS).astype(np.int16).tobytes()


def ulaw_encode(pcm: bytes) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    sign = (x < 0).astype(np.int32) << 7
    m = np.minimum(np.abs(x), _CLIP) + _BIAS
    exp = np.floor(np.log2(m)).astype(np.int32) - 7
    man = (m >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | man) & 0xFF).astype(np.uint8).tobytes()


class Recorder:
    """Both sides on one 24 kHz timeline, mixed when saved."""

    def __init__(self):
        self.t0, self.parts = time.time(), []

    def add(self, pcm24: bytes, at: float):
        if pcm24:
            self.parts.append((max(0, int((at - self.t0) * 24000)), np.frombuffer(pcm24, dtype=np.int16)))

    def pcm(self) -> bytes:
        if not self.parts:
            return b""
        out = np.zeros(max(s + len(a) for s, a in self.parts), dtype=np.int32)
        for s, a in self.parts:
            out[s:s + len(a)] += a
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------- lines (where the person is)

class Line:
    """mic: queue of 16 kHz PCM from the person (None = they hung up). play() takes 24 kHz agent audio."""

    def __init__(self):
        self.mic: asyncio.Queue = asyncio.Queue()
        self.busy_until = 0.0
        self.closed = False

    def _played(self, pcm24) -> float:
        start = max(time.time(), self.busy_until)
        self.busy_until = start + len(pcm24) / 48000
        return start

    async def play(self, pcm24: bytes) -> float: ...
    async def clear(self): self.busy_until = time.time()
    async def caption(self, role, text): pass
    async def hangup(self, outcome=None): self.closed = True


class BrowserPhone(Line):
    def __init__(self, ws: WebSocket):
        super().__init__()
        self.ws, self.answer = ws, None  # answer: Future[bool] while ringing
        self.in_call = False

    async def send(self, obj):
        try:
            await self.ws.send_text(json.dumps(obj))
        except Exception:  # noqa: BLE001  page went away
            pass

    async def play(self, pcm24):
        at = self._played(pcm24)
        try:
            await self.ws.send_bytes(pcm24)
        except Exception:  # noqa: BLE001
            pass
        return at

    async def clear(self):
        await super().clear()
        await self.send({"type": "clear"})

    async def caption(self, role, text):
        await self.send({"type": "caption", "role": role, "text": text})

    async def hangup(self, outcome=None):
        if not self.closed:
            await self.send({"type": "ended", "outcome": outcome})
        self.in_call, self.closed = False, True


class TwilioLine(Line):
    def __init__(self, ws: WebSocket, stream_sid: str):
        super().__init__()
        self.ws, self.sid = ws, stream_sid

    async def play(self, pcm24):
        at = self._played(pcm24)
        payload = base64.b64encode(ulaw_encode(resample(pcm24, 24000, 8000))).decode()
        try:
            await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.sid, "media": {"payload": payload}}))
        except Exception:  # noqa: BLE001
            pass
        return at

    async def clear(self):
        await super().clear()
        try:
            await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.sid}))
        except Exception:  # noqa: BLE001
            pass

    async def hangup(self, outcome=None):
        if not self.closed:
            self.closed = True
            try:
                await self.ws.close()  # Twilio moves to <Hangup/>
            except Exception:  # noqa: BLE001
                pass


PHONES: list[BrowserPhone] = []         # browser phone pages on standby
PENDING: dict[int, asyncio.Future] = {}  # twilio call_id -> Future[TwilioLine | None]


# ---------------------------------------------------------------- the call

async def bridge(c, line: Line, rec: Recorder):
    """One Gemini Live session (caller agent with tools, server-side VAD) <-> the person on `line`."""
    from google import genai
    from google.genai import types

    from desk import call as engine
    from desk.prompts import caller_prompt

    client = genai.Client(api_key=engine.load_key(), http_options={"api_version": "v1beta"})
    prompt = caller_prompt(db.public_customer(c.customer), c.brief, c.offers, engine.history_for(c.customer["id"], c.id)) + PHONE_NOTE
    cfg = engine.live_config(prompt, VOICE, engine.TOOLS)
    cfg["realtime_input_config"] = {"automatic_activity_detection": {"silence_duration_ms": SILENCE_MS}}
    terminal = {"renew_contract", "schedule_callback", "transfer_to_human", "mark_do_not_call"}

    async with client.aio.live.connect(model=engine.MODEL, config=cfg) as s:
        async def up():
            while True:
                pcm = await line.mic.get()
                if pcm is None:
                    return "customer hung up"
                rec.add(resample(pcm, 16000, 24000), time.time() - len(pcm) / 32000)
                await s.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

        async def down():
            heard, said, nudged = "", "", False
            muted = False  # set when the agent starts speaking a function call: drop the rest of that turn

            async def flush(role):
                nonlocal heard, said
                t = (heard if role == "customer" else said).strip()
                if role == "customer":
                    heard = ""
                else:
                    said = ""
                if t:
                    db.add_transcript(c.id, role, t)
                    await line.caption(role, t)

            while True:
                got = 0
                async for msg in s.receive():
                    got += 1
                    if msg.tool_call:
                        await flush("customer")
                        await flush("agent")
                        resps = [types.FunctionResponse(id=fc.id, name=fc.name, response=c.run_tool(fc.name, fc.args))
                                 for fc in msg.tool_call.function_calls]
                        await s.send_tool_response(function_responses=resps)
                        continue
                    sc = msg.server_content
                    if not sc:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        heard += sc.input_transcription.text
                    if sc.interrupted:
                        await line.clear()
                        if said.strip():
                            said = said.rstrip() + " ..."
                        await flush("agent")
                    if sc.output_transcription and sc.output_transcription.text:
                        await flush("customer")
                        said = engine.join_text(said, sc.output_transcription.text)
                        if not muted and LEAK.search(said):
                            muted = True
                            await line.clear()  # cut whatever of it is still queued on the phone
                    if sc.model_turn and not muted:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                rec.add(p.inline_data.data, await line.play(p.inline_data.data))
                    if sc.turn_complete:
                        leak = said if muted else ""
                        if muted:
                            said = strip_leak(said)
                        await flush("customer")
                        await flush("agent")
                        break
                if not got:
                    return "session closed"
                if muted:
                    muted = False
                    name, args = parse_leak(leak)
                    log.warning("call %s: agent spoke a function call: %r", c.id, leak[:200])
                    db.emit("tool", call_id=c.id, customer_id=c.customer["id"], payload={"name": "spoken_call_blocked", "args": {"text": leak[:200]}})
                    if name:
                        res = c.run_tool(name, args)
                        if name == "end_call" or c.end:
                            await asyncio.sleep(max(0, line.busy_until - time.time()) + 0.6)
                            return "ended"
                        await s.send_realtime_input(text=(
                            f"(system: you said a function call out loud instead of calling it. The customer did not hear it. "
                            f"It has been run for you: {name} -> {json.dumps(res)}. Never say function names or arguments. "
                            f"Continue naturally with one or two short sentences.)"))
                    else:
                        await s.send_realtime_input(text=(
                            "(system: you said a function name out loud instead of calling it. The customer did not hear it. "
                            "Call the function properly and silently now, then continue naturally.)"))
                    continue
                if c.end or (terminal & set(c.fired) and nudged):
                    await asyncio.sleep(max(0, line.busy_until - time.time()) + 0.6)  # let the goodbye play out
                    return "ended"
                if terminal & set(c.fired) and not nudged:
                    nudged = True
                    await s.send_realtime_input(text="(system: the matter is settled. If you have not said goodbye, say it in one short sentence, then call end_call now.)")

        await s.send_realtime_input(text="(The person has just answered your call. Start now with your first turn.)")
        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()  # surface errors
        finally:
            for t in tasks:
                t.cancel()


async def run_phone_call(call_id: int, get_line, via: str) -> dict:
    """Same contract as call.run_call, but the customer is a real person reached through get_line()."""
    from desk import call as engine

    t0 = time.time()
    c = engine.Call(call_id)
    cid = c.customer["id"]
    detail, outcome, status, audio_path, capped, line = {}, "failed", "failed", None, False, None
    STATE["active_call_id"] = call_id
    try:
        db.update("calls", call_id, status="live", started_at=db.now_iso())
        db.emit("call_started", call_id=call_id, customer_id=cid, payload={"mode": "phone", "via": via})
        if not c.brief:
            bid = engine.default_brief(cid)
            db.update("calls", call_id, brief_id=bid)
            c.brief = db.get("briefs", bid)
        line = await get_line()
        if line is None:
            outcome, status, detail = "no_answer", "ended", {"note": "No answer on the demo phone"}
        else:
            rec = Recorder()
            try:
                await asyncio.wait_for(bridge(c, line, rec), timeout=CAP_S)
            except asyncio.TimeoutError:
                capped = True
            pcm = rec.pcm()
            if pcm:
                audio_path = str(ROOT / "out" / "calls" / f"call_{call_id}.wav")
                engine.save_wav(audio_path, pcm)
            outcome, status = c.outcome(), "ended"
            detail = {"offer_id": None, "discount_pct": None, "callback_at": None, "reason": None, "note": None}
            detail.update(c.detail)
            detail["note"] = (c.end or {}).get("note") or c.detail.get("note") or (
                "ended at the time cap" if capped else "ended without end_call (the person hung up?)")
    except Exception as e:  # noqa: BLE001
        log.exception("phone call %s failed", call_id)
        outcome, status, detail = "failed", "failed", {"note": f"{type(e).__name__}: {e}"[:300]}
    finally:
        if line:
            await line.hangup(outcome)
        STATE["active_call_id"] = None
    dur = round(time.time() - t0, 1)
    detail["via"] = via
    db.update("calls", call_id, status=status, outcome=outcome, outcome_detail=detail, ended_at=db.now_iso(),
              duration_s=dur, audio_path=audio_path)
    db.emit("call_ended", call_id=call_id, customer_id=cid, payload={"outcome": outcome, "duration_s": dur})
    return {"call_id": call_id, "outcome": outcome, "outcome_detail": detail, "duration_s": dur}


# ---------------------------------------------------------------- dialling

def _customer_name(call_id):
    return db.get("customers", db.get("calls", call_id)["customer_id"])["name"]


def browser_dialer(phone: BrowserPhone, call_id: int):
    async def get_line():
        loop = asyncio.get_running_loop()
        phone.answer, phone.closed = loop.create_future(), False
        while not phone.mic.empty():
            phone.mic.get_nowait()
        await phone.send({"type": "ring", "call_id": call_id, "caller": "Helios Mobile", "customer": _customer_name(call_id)})
        try:
            ok = await asyncio.wait_for(asyncio.shield(phone.answer), RING_S)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            await phone.send({"type": "ended", "outcome": "no_answer"})
            return None
        phone.in_call = True
        await phone.send({"type": "connected"})
        return phone
    return get_line


def twilio_ready():
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")) and bool(STATE["public_url"])


def twilio_dialer(call_id: int, to: str):
    async def get_line():
        import httpx
        sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        pub = STATE["public_url"]
        ws_url = pub.replace("https://", "wss://", 1) + "/api/phone/twilio/ws"
        twiml = (f'<Response><Connect><Stream url="{ws_url}"><Parameter name="call_id" value="{call_id}"/>'
                 f'<Parameter name="k" value="{TOKEN}"/></Stream></Connect><Hangup/></Response>')
        fut = asyncio.get_running_loop().create_future()
        PENDING[call_id] = fut
        try:
            async with httpx.AsyncClient(timeout=15) as h:
                r = await h.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", auth=(sid, tok), data={
                    "To": to, "From": os.environ["TWILIO_FROM"], "Twiml": twiml, "Timeout": 30,
                    "StatusCallback": f"{pub}/api/phone/twilio/status?k={TOKEN}&call_id={call_id}"})
            if r.status_code >= 300:
                raise RuntimeError(f"Twilio refused the call: {r.status_code} {r.text[:200]}")
            return await asyncio.wait_for(fut, TWILIO_WAIT_S)
        except asyncio.TimeoutError:
            return None
        finally:
            PENDING.pop(call_id, None)
    return get_line


DEMO_NAME = os.environ.get("PHONE_CUSTOMER_NAME", "Panos Vardalas")


def demo_customer_id():
    r = db.one("SELECT id FROM customers WHERE name=?", (DEMO_NAME,))
    return r["id"] if r else None


def is_demo(customer_id) -> bool:
    return customer_id == demo_customer_id()


def ensure_demo_customer():
    """Existing databases predate the demo customer: add it from the seed list if missing."""
    from datetime import datetime, timedelta
    if demo_customer_id():
        return
    row = next((c for c in db.CUSTOMERS if c[0] == DEMO_NAME), None)
    if not row:
        return
    name, age, city, plan, price, days, tenure, gb, mins, complaints, pay, notes, persona = row
    nid = (db.one("SELECT COALESCE(MAX(id),0) m FROM customers")["m"]) + 1
    db.insert("customers", id=nid, name=name, phone=f"+30 690 555 {nid:04d}", age=age, city=city, plan_id=plan,
              monthly_price=price, contract_end=(datetime.now().date() + timedelta(days=days)).isoformat(),
              tenure_months=tenure, data_gb_used_avg=gb, minutes_used_avg=mins, complaints=complaints,
              payment_history=pay, notes=notes, persona=persona)


def pick_dialer(call_id):
    """Browser phone if one is open, else Twilio if configured, else None (caller falls back to a simulated call)."""
    idle = [p for p in PHONES if not p.in_call]
    if idle:
        return browser_dialer(idle[-1], call_id), "browser"
    if twilio_ready() and os.environ.get("PHONE_TO"):
        return twilio_dialer(call_id, os.environ["PHONE_TO"]), "twilio"
    return None, None


def route(engine):
    """Wrap a call engine: the demo customer's call rings the real phone; everyone else uses `engine`."""
    async def run(call_id, mode="sim"):
        call = db.get("calls", call_id)
        if is_demo(call["customer_id"]) and not STATE["active_call_id"]:
            dial, via = pick_dialer(call_id)
            if dial:
                return await run_phone_call(call_id, dial, via)
            log.warning("demo customer called but no phone is reachable: simulating instead")
        return await engine(call_id, mode)
    return run


def _mask(num):
    return (num[:4] + "*" * max(0, len(num) - 7) + num[-3:]) if num else None


# ---------------------------------------------------------------- routes

def _from_tunnel(request) -> bool:
    h = request.headers
    return "cf-ray" in h or "cf-connecting-ip" in h or h.get("host", "").endswith(".trycloudflare.com")


def phone_url():
    return f"{STATE['public_url']}/phone?k={TOKEN}" if STATE["public_url"] else None


@router.get("/phone")
def phone_page(k: str = ""):
    if k != TOKEN:
        raise HTTPException(403, detail={"error": "bad or missing phone token"})
    return FileResponse(PHONE_HTML, headers={"Cache-Control": "no-store"})


@router.get("/api/phone/status")
def phone_status():
    return {"browser_phones": len(PHONES), "twilio": twilio_ready(), "public_url": STATE["public_url"],
            "phone_url": phone_url(), "default_to": _mask(os.environ.get("PHONE_TO")), "active_call_id": STATE["active_call_id"]}


@router.get("/api/phone/qr.svg")
def phone_qr():
    url = phone_url()
    if not url:
        raise HTTPException(404, detail={"error": "no public url: start with PHONE_TUNNEL=1 or set PUBLIC_URL"})
    import segno
    import io
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/api/phone/call")
async def phone_call(request: Request):
    from desk import orchestrator
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        d = {}
    via = d.get("via") or "browser"
    cust = db.get("customers", int(d.get("customer_id") or 0))
    if not cust:
        raise HTTPException(404, detail={"error": "no such customer"})
    if cust["status"] == "do_not_call":
        raise HTTPException(409, detail={"error": "customer is on do-not-call"})
    if STATE["active_call_id"]:
        raise HTTPException(409, detail={"error": f"a phone call is already running (call {STATE['active_call_id']})"})
    if via == "browser":
        idle = [p for p in PHONES if not p.in_call]
        if not idle:
            raise HTTPException(409, detail={"error": "no demo phone open", "phone_url": phone_url()})
        phone = idle[-1]
    elif via == "twilio":
        to = d.get("to") or os.environ.get("PHONE_TO")
        if not twilio_ready() or not to:
            raise HTTPException(409, detail={"error": "Twilio not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, PHONE_TO, public url)"})
    else:
        raise HTTPException(400, detail={"error": "via must be browser or twilio"})

    brief = db.one("SELECT * FROM briefs WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cust["id"],))
    earlier = db.one("SELECT COUNT(*) n FROM calls WHERE customer_id=? AND status='ended'", (cust["id"],))["n"]
    call_id = orchestrator._make_call(cust["id"], brief["campaign_id"] if brief else None, "followup" if earlier else "first")
    STATE["active_call_id"] = call_id
    dial = browser_dialer(phone, call_id) if via == "browser" else twilio_dialer(call_id, to)

    async def engine(cid, mode):
        return await run_phone_call(cid, dial, via)

    async def runner():
        try:
            await orchestrator._run_and_handle(call_id, "phone", asyncio.Semaphore(1), engine)
        except Exception:  # noqa: BLE001
            log.exception("phone call %s crashed", call_id)
        finally:
            STATE["active_call_id"] = None

    t = asyncio.create_task(runner())
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return {"call_id": call_id, "via": via}


_TASKS: set = set()


@router.websocket("/api/phone/ws")
async def phone_ws(ws: WebSocket, k: str = ""):
    if k != TOKEN:
        await ws.close(code=4403)
        return
    await ws.accept()
    phone = BrowserPhone(ws)
    PHONES.append(phone)
    await phone.send({"type": "ready"})
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if m.get("bytes") is not None:
                if phone.in_call:
                    phone.mic.put_nowait(m["bytes"])
                continue
            try:
                msg = json.loads(m.get("text") or "{}")
            except ValueError:
                continue
            kind = msg.get("type")
            if kind in ("answer", "decline") and phone.answer and not phone.answer.done():
                phone.answer.set_result(kind == "answer")
            elif kind == "hangup" and phone.in_call:
                phone.mic.put_nowait(None)
    except WebSocketDisconnect:
        pass
    finally:
        PHONES.remove(phone)
        if phone.answer and not phone.answer.done():
            phone.answer.set_result(False)
        phone.closed = True
        phone.mic.put_nowait(None)


@router.websocket("/api/phone/twilio/ws")
async def twilio_ws(ws: WebSocket):
    await ws.accept()
    line = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")
            if ev == "start":
                p = msg["start"].get("customParameters") or {}
                fut = PENDING.get(int(p.get("call_id") or 0))
                if p.get("k") != TOKEN or not fut or fut.done():
                    await ws.close(code=4403)
                    return
                line = TwilioLine(ws, msg["start"]["streamSid"])
                fut.set_result(line)
            elif ev == "media" and line:
                pcm8 = ulaw_decode(base64.b64decode(msg["media"]["payload"]))
                line.mic.put_nowait(resample(pcm8, 8000, 16000))
            elif ev == "stop":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if line:
            line.closed = True
            line.mic.put_nowait(None)


@router.post("/api/phone/twilio/status")
async def twilio_status(request: Request, k: str = "", call_id: int = 0):
    if k != TOKEN:
        raise HTTPException(403, detail={"error": "bad token"})
    form = {a: v[0] for a, v in parse_qs((await request.body()).decode()).items()}
    fut = PENDING.get(call_id)
    log.info("twilio call %s status %s", call_id, form.get("CallStatus"))
    if fut and not fut.done():
        fut.set_result(None)  # finished (no-answer, busy, failed, or completed) before the stream connected
    return Response(status_code=204)


# ---------------------------------------------------------------- tunnel + wiring

async def start_tunnel(port: int):
    """cloudflared quick tunnel: free, no account, new https://*.trycloudflare.com URL every start."""
    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    STATE["tunnel"] = proc

    async def read():
        while line := await proc.stderr.readline():
            m = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not STATE["public_url"]:
                STATE["public_url"] = m.group(0).decode()
                log.warning("demo phone: open %s on your mobile", phone_url())
                print(f"\n  Demo phone URL (open on your mobile): {phone_url()}\n", flush=True)
    _TASKS.add(asyncio.create_task(read()))


def mount(app):
    app.include_router(router)

    @app.middleware("http")
    async def tunnel_guard(request: Request, call_next):
        if _from_tunnel(request) and not request.url.path.startswith(PUBLIC_PATHS):
            return JSONResponse({"error": "not available over the public tunnel"}, status_code=403)
        return await call_next(request)

    @app.on_event("startup")
    async def _phone_startup():
        ensure_demo_customer()
        if os.environ.get("PHONE_TUNNEL") == "1" and not STATE["public_url"]:
            try:
                await start_tunnel(int(os.environ.get("DESK_PORT", 8765)))
            except FileNotFoundError:
                log.error("PHONE_TUNNEL=1 but cloudflared is not installed")
        elif STATE["public_url"]:
            print(f"\n  Demo phone URL (open on your mobile): {phone_url()}\n", flush=True)

    @app.on_event("shutdown")
    async def _phone_shutdown():
        p = STATE.get("tunnel")
        if p and p.returncode is None:
            p.terminate()
