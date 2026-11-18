"""Live voice calls: the caller agent (Gemini Live, server-side voice activity detection) talks to a real person.

Two ways to reach the person:
  browser  open /phone?k=TOKEN on a mobile (HTTPS through a cloudflared tunnel). Several phones may be open,
           each is one line and carries one call at a time, so calls can run in parallel.
  twilio   a real PSTN call via Twilio Media Streams.

Env: PHONE_TUNNEL=1 (cloudflared quick tunnel at boot), PUBLIC_URL, PHONE_TOKEN (default random per boot),
PHONE_CAP_S (180), PHONE_VOICE (Kore), PHONE_SILENCE_MS (500), TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
TWILIO_FROM, PHONE_TO. Numbers from the customer file are dialled ONLY with PHONE_ALLOW_CUSTOMER_NUMBERS=1.
best_move is declared NON_BLOCKING (set PROVA_NONBLOCKING=0 to make it blocking).

Anything reaching the server through the tunnel may only use the phone paths below, and needs the token.
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from urllib.parse import parse_qs

import numpy as np
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from prova import db, llm

log = logging.getLogger("prova.voice")
ROOT = db.ROOT
PHONE_HTML = ROOT / "ui" / "phone.html"
MODEL = "gemini-3.1-flash-live-preview"
CAP_S = float(os.environ.get("PHONE_CAP_S", 180))
VOICE = os.environ.get("PHONE_VOICE", "Kore")
SILENCE_MS = int(os.environ.get("PHONE_SILENCE_MS", 500))
RING_S = 35
FAKE_RING_S = 5   # FAKE mode: how long a consent-page socket may take to answer
BIND_WAIT_S = 20   # how long a consent-page call waits for the customer's own socket
TWILIO_WAIT_S = 90
TOKEN = os.environ.get("PHONE_TOKEN") or secrets.token_urlsafe(9)
STATE = {"public_url": os.environ.get("PUBLIC_URL", "").rstrip("/") or None, "active": set(), "tunnel": None,
         "tool_ms": [], "timeline": [],
         # tunnel: off (none started) | fixed (PUBLIC_URL given) | starting | up | down (lost, being reopened) | missing
         "tunnel_state": "fixed" if os.environ.get("PUBLIC_URL") else "off", "tunnel_metrics": None,
         "tunnel_started": 0.0, "tunnel_port": None, "tunnel_restarts": 0}
TUNNEL_GRACE_S = 30   # a new tunnel gets this long to connect before it is judged
TUNNEL_DOWN_S = 20    # this long with no connection to Cloudflare = the tunnel is gone, open a new one
PUBLIC_PATHS = ("/phone", "/phone.html", "/api/phone/ws", "/api/phone/twilio/ws", "/api/phone/twilio/status", "/favicon.ico",
                "/c/", "/k/", "/join", "/inbox/", "/api/inbox/", "/t/")  # customer confirmation links must open from outside
TERMINAL = {"record_agreement", "record_acknowledgement", "record_booking", "record_promise", "schedule_callback",
            "transfer_to_human", "mark_do_not_call"}

router = APIRouter()

# Native-audio models sometimes SPEAK a function call instead of making it. Detect it in the agent's transcript,
# mute the rest of that turn, run it for real.
_TOOL_NAMES = ("get_offers|verify_identity|get_account_details|quote_price|record_agreement|record_acknowledgement|"
               "record_booking|record_promise|schedule_callback|transfer_to_human|mark_do_not_call|best_move|end_call")
LEAK = re.compile(rf"\b(?:{_TOOL_NAMES})\b|\b(?:get|quote|record|schedule|transfer|mark|end|verify|best)[ _][a-z_ ]{{0,20}}\(", re.I)
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
        if v and re.fullmatch(r"-?\d+(\.\d+)?", v):
            val = float(v)
        args[k] = val
    return m.group(1), args


def strip_leak(text):
    m = LEAK.search(text)
    return text[:m.start()].rstrip() if m else text


def join_text(prev: str, frag: str) -> str:
    if prev and frag and not prev[-1].isspace() and not frag[0].isspace() and prev[-1] in ".?!,:;" and frag[0].isalnum():
        return prev + " " + frag
    return prev + frag


# ---------------------------------------------------------------- audio helpers

def resample(pcm: bytes, src: int, dst: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(x) < 2 or src == dst:
        return pcm
    if src == 3 * dst:
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


def save_wav(path, pcm, rate=24000):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


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
    """mic: queue of 16 kHz PCM bytes from the person; a str item is typed text standing in for speech
    (used by tests); None = they hung up. play() takes 24 kHz agent audio."""

    def __init__(self):
        self.mic: asyncio.Queue = asyncio.Queue()
        self.busy_until = 0.0
        self.closed = False

    def _played(self, pcm24) -> float:
        start = max(time.time(), self.busy_until)
        self.busy_until = start + len(pcm24) / 48000
        return start

    async def play(self, pcm24: bytes) -> float:
        return self._played(pcm24)

    async def clear(self):
        self.busy_until = time.time()

    async def caption(self, role, text):
        pass

    async def hangup(self, outcome=None):
        self.closed = True


class BrowserPhone(Line):
    def __init__(self, ws: WebSocket):
        super().__init__()
        self.ws, self.answer = ws, None
        self.in_call = False
        self.busy = False      # reserved by a call (ringing or talking)
        self.customer_id = None  # set when the socket was opened from a consent page (?ct=): it belongs to that customer only

    async def send(self, obj):
        with contextlib.suppress(Exception):
            await self.ws.send_text(json.dumps(obj))

    async def play(self, pcm24):
        at = self._played(pcm24)
        with contextlib.suppress(Exception):
            await self.ws.send_bytes(pcm24)
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
        with contextlib.suppress(Exception):
            await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.sid, "media": {"payload": payload}}))
        return at

    async def clear(self):
        await super().clear()
        with contextlib.suppress(Exception):
            await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.sid}))

    async def hangup(self, outcome=None):
        if not self.closed:
            self.closed = True
            with contextlib.suppress(Exception):
                await self.ws.close()


PHONES: list[BrowserPhone] = []
PENDING: dict[int, asyncio.Future] = {}
_TASKS: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


# ---------------------------------------------------------------- the Live session

def _nonblocking() -> bool:
    return os.environ.get("PROVA_NONBLOCKING", "1") != "0"


def _declarations(purpose: str) -> list:
    """gateway.tools_for(purpose) as a Live API tools list; best_move NON_BLOCKING unless PROVA_NONBLOCKING=0."""
    from prova import gateway
    tools = gateway.tools_for(purpose)
    if tools and "function_declarations" not in tools[0]:
        tools = [{"function_declarations": list(tools)}]
    out = []
    for t in tools:
        decls = []
        for d in t["function_declarations"]:
            d = dict(d)
            if d["name"] == "best_move" and _nonblocking():
                d["behavior"] = "NON_BLOCKING"
            decls.append(d)
        out.append({"function_declarations": decls})
    return out


_BCP47 = {"el": "el-GR", "en": "en-US", "de": "de-DE", "fr": "fr-FR", "it": "it-IT", "es": "es-ES"}


def live_config(prompt: str, tools: list) -> dict:
    # Tell the session which language is spoken. Left on auto-detect, short Greek answers (a year, a number)
    # were transcribed as Spanish, and the transcript is what the tools and the sidecar act on.
    lang = str((db.slots().get("language") or "el")).lower()
    code = _BCP47.get(lang[:2], lang)
    return {
        "response_modalities": ["AUDIO"],
        "system_instruction": prompt,
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": VOICE}}, "language_code": code},
        "input_audio_transcription": {"language_codes": [code]},
        "output_audio_transcription": {"language_codes": [code]},
        "realtime_input_config": {"automatic_activity_detection": {"silence_duration_ms": SILENCE_MS}},
        "tools": tools,
    }


def _history(customer_id: int, call_id: int) -> list[dict]:
    rows = db.q("SELECT id, outcome, summary, ended_at FROM call WHERE customer_id=? AND id<>? AND mode='live' "
                "AND summary IS NOT NULL ORDER BY id", (customer_id, call_id))
    return [{"call_id": r["id"], "outcome": r["outcome"], "summary": r["summary"], "started_at": r["ended_at"]} for r in rows]


def _cards(brief: dict | None) -> list[dict]:
    ids = list((brief or {}).get("tactic_card_ids") or [])
    eid = (brief or {}).get("explore_card_id")
    if eid and eid not in ids:
        ids.append(eid)
    return [c for c in (db.get("tactic_card", i) for i in ids) if c]


async def bridge(ctx, line: Line, rec: Recorder | None, sidecar) -> str:
    """One Gemini Live session <-> the person on `line`. Returns why it ended."""
    from google import genai
    from google.genai import types

    from prova import prompts

    call = db.get("call", ctx.call_id)
    brief = call.get("brief")
    cid, cust_id = ctx.call_id, ctx.customer["id"]
    client = genai.Client(api_key=llm.load_key(), http_options={"api_version": "v1beta"})
    prompt = prompts.caller_prompt(ctx, brief, _cards(brief), _history(cust_id, cid), channel="voice")
    cfg = live_config(prompt, _declarations(call["purpose"]))
    nonblocking = _nonblocking()
    cut = asyncio.Event()
    cut_why = {"reason": None}

    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        async def inject(text: str):
            await s.send_realtime_input(text=f"(system: {text})")

        async def handle_guard(g: dict):
            a = g["action"]
            if a == "cut":
                if "mark_do_not_call" not in ctx.fired:
                    await ctx.run_tool_async("mark_do_not_call", {})
                await inject("the customer asked not to be called again and it is recorded. Say one short polite goodbye, "
                             "then call end_call. Do not try to sell anything.")
            elif a == "handoff":
                await inject("the customer wants a person. Say in one short sentence that you are handing over, "
                             "then call transfer_to_human now.")
            elif a == "stop_selling":
                await inject("the customer seems confused or vulnerable. Stop selling. Speak slowly and gently, "
                             "offer to have a colleague call them, then call transfer_to_human.")

        async def react_customer(text: str):
            try:
                await sidecar.on_customer_turn(text)
                g = sidecar.pending_guard()
                if g:
                    await handle_guard(g)
            except Exception:  # noqa: BLE001
                log.exception("sidecar customer turn failed")

        async def react_agent(text: str):
            try:
                act = await sidecar.on_agent_turn(text)
                if not act:
                    return
                if act["action"] == "correct":
                    await inject(act["instruction"])
                elif act["action"] == "cut":
                    cut_why["reason"] = act.get("reason")
                    cut.set()
                else:
                    await handle_guard(act)
            except Exception:  # noqa: BLE001
                log.exception("sidecar agent turn failed")

        def note_tools(names, t0):
            ms = round((time.monotonic() - t0) * 1000)
            STATE["tool_ms"].append({"call_id": cid, "tools": names, "ms": ms})
            STATE["timeline"].append((cid, "tool_response", names, time.time()))
            log.info("call %s tools %s answered in %d ms", cid, names, ms)

        async def answer_async(fc, t0):
            try:
                res = await ctx.run_tool_async(fc.name, dict(fc.args or {}))
                await s.send_tool_response(function_responses=[types.FunctionResponse(
                    id=fc.id, name=fc.name, response=res, scheduling=types.FunctionResponseScheduling.WHEN_IDLE)])
                note_tools([fc.name], t0)
            except Exception:  # noqa: BLE001
                log.exception("non-blocking tool %s failed", fc.name)

        async def customer_said(text: str):
            text = text.strip()
            if text:
                db.add_turn(cid, "customer", text)
                await line.caption("customer", text)
                _spawn(react_customer(text))

        async def up():
            while True:
                item = await line.mic.get()
                if item is None:
                    return "customer hung up"
                if isinstance(item, str):
                    await customer_said(item)
                    await s.send_realtime_input(text=item)
                    continue
                if rec:
                    rec.add(resample(item, 16000, 24000), time.time() - len(item) / 32000)
                await s.send_realtime_input(audio=types.Blob(data=item, mime_type="audio/pcm;rate=16000"))

        async def down():
            heard, said, nudged, muted = "", "", False, False

            async def flush(role):
                nonlocal heard, said
                if role == "customer":
                    t, heard = heard.strip(), ""
                    await customer_said(t)
                else:
                    t, said = strip_leak(said).strip() if muted else said.strip(), ""
                    if t:
                        STATE["timeline"].append((cid, "agent_text", t[:50], time.time()))
                        db.add_turn(cid, "agent", t)
                        await line.caption("agent", t)
                        _spawn(react_agent(t))

            while True:
                got = 0
                async for msg in s.receive():
                    got += 1
                    if msg.tool_call:
                        await flush("customer")
                        await flush("agent")
                        t0 = time.monotonic()
                        STATE["timeline"].append((cid, "tool_call", [fc.name for fc in msg.tool_call.function_calls], time.time()))
                        resps = []
                        for fc in msg.tool_call.function_calls:
                            if nonblocking and fc.name == "best_move":
                                _spawn(answer_async(fc, t0))  # the model keeps talking; the answer arrives when ready
                                continue
                            res = await ctx.run_tool_async(fc.name, dict(fc.args or {}))
                            resps.append(types.FunctionResponse(id=fc.id, name=fc.name, response=res))
                        if resps:
                            await s.send_tool_response(function_responses=resps)
                            note_tools([r.name for r in resps], t0)
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
                        said = join_text(said, sc.output_transcription.text)
                        if not muted and LEAK.search(said):
                            muted = True
                            await line.clear()
                    if sc.model_turn and not muted:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                at = await line.play(p.inline_data.data)
                                if rec:
                                    rec.add(p.inline_data.data, at)
                    if sc.turn_complete:
                        leak = said if muted else ""
                        await flush("customer")
                        await flush("agent")
                        break
                if not got:
                    return "session closed"
                if muted:
                    muted = False
                    name, args = parse_leak(leak)
                    log.warning("call %s: agent spoke a function call: %r", cid, leak[:200])
                    db.emit("tool", call_id=cid, customer_id=cust_id,
                            payload={"name": "spoken_call_blocked", "args": {"text": leak[:200]}, "result": None,
                                     "allowed": False, "rule": "spoken_function_call"})
                    if name:
                        res = await ctx.run_tool_async(name, args)
                        if name == "end_call" or ctx.end:
                            await asyncio.sleep(max(0, line.busy_until - time.time()) + 0.6)
                            return "ended"
                        await inject(f"you said a function call out loud instead of calling it. The customer did not hear it. "
                                     f"It has been run for you: {name} -> {json.dumps(res, default=str)}. Never say function "
                                     f"names or arguments. Continue naturally with one or two short sentences.")
                    else:
                        await inject("you said a function name out loud instead of calling it. The customer did not hear it. "
                                     "Call the function properly and silently now, then continue naturally.")
                    continue
                if ctx.end or (TERMINAL & set(ctx.fired) and nudged):
                    await asyncio.sleep(max(0, line.busy_until - time.time()) + 0.6)
                    return "ended"
                if TERMINAL & set(ctx.fired) and not nudged:
                    nudged = True
                    await inject("the matter is settled. If you have not said goodbye, say it in one short sentence, "
                                 "then call end_call now.")

        await s.send_realtime_input(text="(The person has just answered your call. Start now with your first turn.)")
        tasks = [asyncio.create_task(up()), asyncio.create_task(down()), asyncio.create_task(cut.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                r = t.result()
            if cut.is_set():
                return f"cut by sidecar: {cut_why['reason']}"
            return r
        finally:
            for t in tasks:
                t.cancel()


# ---------------------------------------------------------------- dialling

def browser_dialer(phone: BrowserPhone, call_id: int, company: str, customer: str):
    async def get_line():
        loop = asyncio.get_running_loop()
        phone.answer, phone.closed = loop.create_future(), False
        while not phone.mic.empty():
            phone.mic.get_nowait()
        await phone.send({"type": "ring", "call_id": call_id, "company": company, "caller": company, "customer": customer})
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


def _twilio_number(to: str | None, customer: dict) -> str | None:
    if to or os.environ.get("PHONE_TO"):
        return to or os.environ["PHONE_TO"]
    if os.environ.get("PHONE_ALLOW_CUSTOMER_NUMBERS") == "1":
        return customer.get("phone")
    return None


def _idle_phone() -> BrowserPhone | None:
    """A desk phone nobody reserved. Sockets bound to a customer (consent page) are never picked here."""
    idle = [p for p in PHONES if not p.busy and not p.in_call and p.customer_id is None]
    return idle[-1] if idle else None


def bound_phone(customer_id: int) -> BrowserPhone | None:
    """The newest idle browser socket the customer's own consent page opened."""
    mine = [p for p in PHONES if p.customer_id == customer_id and not p.busy and not p.in_call and not p.closed]
    return mine[-1] if mine else None


async def _wait_bound(customer_id: int, timeout: float) -> BrowserPhone | None:
    end = time.monotonic() + timeout
    while True:
        p = bound_phone(customer_id)
        if p or time.monotonic() >= end:
            return p
        await asyncio.sleep(0.25)


def _customer_for_token(ct: str) -> int | None:
    """consent token -> customer id, only while the request is answered 'call now' and fresh."""
    row = db.one("SELECT * FROM consent_request WHERE token=?", (str(ct or ""),)) if ct and len(str(ct)) >= 8 else None
    if not row or row.get("status") != "call_now":
        return None
    try:
        from datetime import datetime
        if (db.now() - datetime.fromisoformat(row["answered_at"])).total_seconds() > 6 * 3600:
            return None
    except (TypeError, ValueError):
        return None
    return row["customer_id"]


def _mask(num):
    return (num[:4] + "*" * max(0, len(num) - 7) + num[-3:]) if num else None


# ---------------------------------------------------------------- the call runner

async def _fake_call(call_id: int) -> dict:
    """FAKE mode: no network session. The text engine runs on the same call row. When the customer's own browser socket
    (consent page, ?ct=) is open, it rings and shows the captions, so the page can be tested end to end."""
    db.update("call", call_id, via="text")
    try:
        from prova import sim
    except ImportError:
        sim = None
    cust_id = (db.get("call", call_id) or {}).get("customer_id")
    phone = bound_phone(cust_id) if cust_id is not None else None
    if phone:
        phone.busy = True
        phone.answer, phone.closed = asyncio.get_running_loop().create_future(), False
        company = db.slots().get("company_name") or "Prova"
        await phone.send({"type": "ring", "call_id": call_id, "company": company, "caller": company,
                          "customer": (db.get("customer", cust_id) or {}).get("name")})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(phone.answer), FAKE_RING_S)
        if phone.answer.done() and phone.answer.result() is True:
            phone.in_call = True
            await phone.send({"type": "connected"})
        else:
            await phone.send({"type": "ended", "outcome": "no_answer"})
            phone.busy = False
            phone = None
    if sim is not None:
        res = await sim.run_text_call(call_id)
    else:
        res = await _scripted_call(call_id)
    if phone:
        for t in db.turns(call_id):
            if t["role"] in ("agent", "customer"):
                await phone.caption(t["role"], t["text"])
        await phone.hangup(res.get("outcome"))
        phone.busy = False
        db.update("call", call_id, via="browser")
        return {**res, "via": "browser"}
    return {**res, "via": "text"}


async def _scripted_call(call_id: int) -> dict:
    """Tiny scripted call through the gateway, used in FAKE mode when sim.py is not there."""
    from prova import gateway, policy
    from prova.sidecar import Sidecar
    ctx = gateway.CallCtx(call_id, dry_run=False)
    brief = db.get("call", call_id).get("brief")
    sc = ctx.sidecar = Sidecar(ctx, brief, "text")
    db.update("call", call_id, status="live", started_at=db.now_iso())
    line = policy.opening_line(ctx.customer["name"], personalised=(brief or {}).get("opening"))
    db.add_turn(call_id, "agent", line)
    await sc.on_agent_turn(line)
    db.add_turn(call_id, "customer", "Θα το σκεφτώ.")
    await sc.on_customer_turn("Θα το σκεφτώ.")
    mv = await ctx.run_tool_async("best_move", {"situation": "wants_to_think", "customer_said": "Θα το σκεφτώ."})
    db.add_turn(call_id, "agent", mv.get("say") or "Εντάξει.")
    await ctx.run_tool_async("end_call", {"outcome": "thinking", "note": "scripted FAKE call"})
    return ctx.finish()


async def run_live_call(call_id: int, via: str = "auto", to: str | None = None) -> dict:
    if llm.FAKE:
        return await _fake_call(call_id)

    from prova import gateway
    from prova.sidecar import Sidecar

    call = db.get("call", call_id)
    ctx = gateway.CallCtx(call_id, dry_run=False)
    cust = ctx.customer
    company = (db.slots().get("company_name") or (db.company() or {}).get("name") or "Prova")

    phone = None
    if via in ("browser", "auto"):
        tapped = str((cust.get("consent") or {}).get("source") or "").startswith("tap")
        if tapped:   # the customer's own page: only its own socket, never a guessed desk phone
            phone = bound_phone(cust["id"]) or await _wait_bound(cust["id"], BIND_WAIT_S)
            if phone is None:
                raise RuntimeError("the customer's browser is not connected")
        else:
            phone = _idle_phone()
        if phone:
            phone.busy = True
            via = "browser"
        elif via == "browser":
            raise RuntimeError("no phone reachable: no browser phone is open")
    if phone is None:
        number = _twilio_number(to, cust)
        if via in ("twilio", "auto") and twilio_ready() and number:
            via = "twilio"
        else:
            raise RuntimeError("no phone reachable")
        dial = twilio_dialer(call_id, number)
    else:
        dial = browser_dialer(phone, call_id, company, cust["name"])

    sidecar = ctx.sidecar = Sidecar(ctx, call.get("brief"), "live")
    t0 = time.time()
    STATE["active"].add(call_id)
    line, audio_path, capped = None, None, False
    status, note, no_answer = "ended", None, False
    try:
        db.update("call", call_id, status="live", via=via, started_at=db.now_iso())
        if not db.one("SELECT id FROM event WHERE type='call_started' AND call_id=?", (call_id,)):
            db.emit("call_started", call_id=call_id, customer_id=cust["id"],
                    payload={"mode": "live", "via": via, "purpose": call.get("purpose")})
        line = await dial()
        if line is None:
            no_answer, note = True, "nobody answered"
        else:
            t0 = time.time()
            rec = Recorder() if (db.slots().get("recording") or {}).get("on") else None
            why = ""
            try:
                why = await asyncio.wait_for(bridge(ctx, line, rec, sidecar), timeout=CAP_S)
            except asyncio.TimeoutError:
                capped = True
            if rec and rec.pcm():
                audio_path = str(ROOT / "out" / "calls" / f"call_{call_id}.wav")
                save_wav(audio_path, rec.pcm())
                db.update("call", call_id, audio_path=audio_path)
            if why.startswith("cut"):
                status, note = "failed", why
            elif not ctx.end and not ctx.fired:
                note = "ended at the time cap" if capped else (why or "ended without end_call")
            dur = time.time() - t0
            db.add_cost("voice", llm.voice_cost(dur), call_id=call_id, model=MODEL, units={"seconds": round(dur, 1)})
    except Exception as e:  # noqa: BLE001
        log.exception("live call %s failed", call_id)
        status, note = "failed", f"{type(e).__name__}: {e}"[:300]
    finally:
        if line:
            await line.hangup("no_answer" if no_answer else None)
        if phone:
            phone.busy = False
        STATE["active"].discard(call_id)
    if no_answer:  # CallCtx.outcome() never yields no_answer on its own
        ctx.outcome = lambda: "no_answer"
    return {**ctx.finish(status=status, note=note), "via": via}


# ---------------------------------------------------------------- routes

def _from_tunnel(request) -> bool:
    h = request.headers
    return "cf-ray" in h or "cf-connecting-ip" in h or h.get("host", "").endswith(".trycloudflare.com")


def phone_url():
    return f"{STATE['public_url']}/phone?k={TOKEN}" if STATE["public_url"] else None


def phone_status() -> dict:
    act = sorted(STATE["active"])
    return {"browser_phones": len(PHONES), "twilio": twilio_ready(), "public_url": STATE["public_url"],
            "phone_url": phone_url(), "tunnel": STATE["tunnel_state"], "tunnel_restarts": STATE["tunnel_restarts"],
            "default_to": _mask(os.environ.get("PHONE_TO")),
            "active_call_id": act[-1] if act else None, "active_call_ids": act}


@router.get("/phone")
def phone_page(k: str = ""):
    if k != TOKEN:
        raise HTTPException(403, detail={"error": "bad or missing phone token"})
    return FileResponse(PHONE_HTML, headers={"Cache-Control": "no-store"})


@router.get("/api/phone/status")
def phone_status_route():
    return phone_status()


@router.get("/api/phone/qr.svg")
def phone_qr():
    url = phone_url()
    if not url:
        raise HTTPException(404, detail={"error": "no public url: start with PHONE_TUNNEL=1 or set PUBLIC_URL"})
    import io

    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.websocket("/api/phone/ws")
async def phone_ws(ws: WebSocket, k: str = "", ct: str = ""):
    """k = the shared desk-phone token; ct = a consent token from /k/{token} or /join (binds the socket to that customer)."""
    bound = None
    if ct:
        bound = _customer_for_token(ct)
        if bound is None:
            await ws.close(code=4403)
            return
    elif k != TOKEN:
        await ws.close(code=4403)
        return
    await ws.accept()
    phone = BrowserPhone(ws)
    phone.customer_id = bound
    PHONES.append(phone)
    await phone.send({"type": "ready", "company": (db.slots().get("company_name") if db.company() else None)})
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
        if phone in PHONES:
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
        fut.set_result(None)
    return Response(status_code=204)


# ---------------------------------------------------------------- tunnel + wiring

def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


async def start_tunnel(port: int):
    """cloudflared quick tunnel: free, no account, new https://*.trycloudflare.com URL every start.

    Quick tunnels can vanish at any time while cloudflared keeps running, so tunnel_watch() asks cloudflared's own
    /ready endpoint how many connections it holds and opens a new tunnel when that stays at zero.
    """
    old = STATE.get("tunnel")
    if old and old.returncode is None:
        old.kill()  # not terminate(): a hung cloudflared would ignore it
        _spawn(old.wait())
    metrics = _free_port()
    STATE.update(public_url=None, tunnel_state="starting", tunnel_metrics=metrics, tunnel_port=port,
                 tunnel_started=time.monotonic())
    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--no-autoupdate", "--metrics", f"127.0.0.1:{metrics}",
        "--url", f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    STATE["tunnel"] = proc

    async def read():
        while line := await proc.stderr.readline():
            m = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and STATE.get("tunnel") is proc and not STATE["public_url"]:
                STATE["public_url"] = m.group(0).decode()
                print(f"\n  Demo phone URL (open on your mobile): {phone_url()}\n", flush=True)
    _spawn(read())


def _edge_connections(metrics_port: int) -> int:
    """How many connections to Cloudflare this cloudflared holds right now (0 = nobody can reach us)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/ready", timeout=2) as r:
            body = r.read()
    except urllib.error.HTTPError as e:  # cloudflared answers 503 with the same JSON body when it has none
        body = e.read()
    except OSError:
        return 0
    try:
        return int(json.loads(body).get("readyConnections") or 0)
    except (ValueError, AttributeError):
        return 0


async def restart_tunnel(reason: str = "") -> dict:
    """Throw the tunnel away and open a new one (new public URL: phones must scan the new QR code)."""
    if os.environ.get("PUBLIC_URL"):
        return {"ok": False, "error": "the public URL is fixed by PUBLIC_URL; nothing to restart"}
    port = STATE.get("tunnel_port") or int(os.environ.get("PROVA_PORT", 8770))
    try:
        await start_tunnel(port)
    except FileNotFoundError:
        STATE["tunnel_state"] = "missing"
        return {"ok": False, "error": "cloudflared is not installed"}
    STATE["tunnel_restarts"] += 1
    log.warning("phone tunnel reopened (%s)", reason or "requested")
    return {"ok": True}


async def tunnel_watch(every: float = 5.0):
    """Keep the phone link alive: notice a dead tunnel and replace it."""
    lost_since = None
    while True:
        await asyncio.sleep(every)
        proc = STATE.get("tunnel")
        if not proc:
            continue
        try:
            age = time.monotonic() - STATE["tunnel_started"]
            exited = proc.returncode is not None
            n = 0 if exited else await asyncio.to_thread(_edge_connections, STATE["tunnel_metrics"])
            if n and STATE["public_url"]:
                STATE["tunnel_state"], lost_since = "up", None
                continue
            if age < TUNNEL_GRACE_S and not exited:
                continue  # still starting
            lost_since = lost_since or time.monotonic()
            STATE["tunnel_state"] = "down"
            if exited or time.monotonic() - lost_since >= TUNNEL_DOWN_S:
                lost_since = None
                await restart_tunnel("cloudflared exited" if exited else "no connection to Cloudflare")
        except Exception:  # noqa: BLE001 - the watcher must never die
            log.exception("tunnel watch")


@router.post("/api/phone/tunnel")
async def tunnel_restart_route():
    """Open a fresh phone link now. Local only: the tunnel guard blocks this path from outside."""
    r = await restart_tunnel("requested from the desk")
    if not r["ok"]:
        raise HTTPException(400, detail={"error": r["error"]})
    return phone_status()


def _owner_token(request) -> bool:
    """The owner's PROVA_TOKEN (cookie from /login, bearer or header) opens the full desk over the tunnel."""
    import hmac as _h
    from prova.guard import auth as _a
    t, g = _a.token(), _a._given(request)
    return bool(t and g and _h.compare_digest(g, t))


def mount(app):
    app.include_router(router)

    @app.middleware("http")
    async def tunnel_guard(request: Request, call_next):
        if _from_tunnel(request) and not request.url.path.startswith(PUBLIC_PATHS + ("/login",)) and not _owner_token(request):
            return JSONResponse({"error": "not available over the public tunnel"}, status_code=403)
        return await call_next(request)

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        if os.environ.get("PHONE_TUNNEL") == "1" and not STATE["public_url"]:
            try:
                await start_tunnel(int(os.environ.get("PROVA_PORT", 8770)))
            except FileNotFoundError:
                STATE["tunnel_state"] = "missing"
                log.error("PHONE_TUNNEL=1 but cloudflared is not installed")
        elif STATE["public_url"]:
            print(f"\n  Demo phone URL (open on your mobile): {phone_url()}\n", flush=True)
        watch = _spawn(tunnel_watch())
        try:
            async with inner(a):
                yield
        finally:
            watch.cancel()
            p = STATE.get("tunnel")
            if p and p.returncode is None:
                p.terminate()

    app.router.lifespan_context = lifespan
