# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai>=1.0", "numpy"]
# ///
"""Voice sales call on the Gemini Live API (speech-to-speech). Nothing heavy runs locally.

  uv run live.py talk              # live call: you are the lead. USE HEADPHONES.
  uv run live.py script            # no mic: sends the scripted lead lines as text, times replies, saves wavs
  uv run live.py talk --model gemini-3.1-flash-live-preview --voice Puck

Key: GEMINI_API_KEY in .env (gitignored) or the environment.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types

from prompts import LEAD_LINES, SYSTEM

ROOT = Path(__file__).parent
OUT = ROOT / "out"
IN_RATE, OUT_RATE = 16000, 24000
CHUNK = 1600  # 50 ms of 16-bit mono at 16 kHz
now = time.perf_counter


def load_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY missing: put it in .env")
    return key


def make_config(args):
    return {
        "response_modalities": ["AUDIO"],
        "system_instruction": SYSTEM,
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": args.voice}}},
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "realtime_input_config": {
            "automatic_activity_detection": {"disabled": True} if getattr(args, "ptt", False)
            else {"silence_duration_ms": args.silence_ms}
        },
    }


def save_wav(path, pcm, rate):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class Player:
    """Raw PCM out through aplay. flush() drops queued audio when the lead interrupts."""

    def __init__(self):
        self.proc = None
        self.busy_until = 0.0

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-f", "S16_LE", "-r", str(OUT_RATE), "-c", "1", "-t", "raw",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    async def write(self, pcm):
        self.busy_until = max(now(), self.busy_until) + len(pcm) / (2 * OUT_RATE)
        self.proc.stdin.write(pcm)
        await self.proc.stdin.drain()

    async def flush(self):
        self.busy_until = 0.0
        await self.close()
        await self.start()

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


class Stats:
    def __init__(self):
        self.turns, self.tokens = [], 0

    def usage(self, msg):
        u = getattr(msg, "usage_metadata", None)
        if u and u.total_token_count:
            self.tokens = max(self.tokens, u.total_token_count)

    def report(self, model, started):
        lat = [t["latency_ms"] for t in self.turns if t.get("latency_ms") is not None]
        print(f"\n=== {model}: {len(self.turns)} agent turns in {now() - started:.0f}s ===")
        if lat:
            print(f"reply latency ms: median {statistics.median(lat):.0f}  min {min(lat)}  max {max(lat)}")
        print(f"tokens reported by API: {self.tokens}")
        OUT.mkdir(exist_ok=True)
        (OUT / f"live_transcript_{model}_{time.strftime("%H%M%S")}.json").write_text(json.dumps({"model": model, "turns": self.turns}, indent=2))
        print(f"transcript: {OUT}/live_transcript_{model}_{time.strftime("%H%M%S")}.json")


async def receive_turn(session, stats, on_audio=None, on_interrupt=None, t_ref=None, live=False):
    """Consume one model turn. t_ref() gives the moment the lead stopped talking."""
    heard, said, pcm, t_first = "", "", bytearray(), None
    async for msg in session.receive():
        stats.usage(msg)
        sc = msg.server_content
        if not sc:
            continue
        if sc.interrupted:
            if on_interrupt:
                await on_interrupt()
        if sc.input_transcription and sc.input_transcription.text:
            heard += sc.input_transcription.text
        if sc.output_transcription and sc.output_transcription.text:
            said += sc.output_transcription.text
        if sc.model_turn:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    if t_first is None:
                        t_first = now()
                        ref = t_ref() if t_ref else None
                        latency = round((t_first - ref) * 1000) if ref else None
                        if live:
                            print(f"  [agent speaking, latency {latency} ms]", flush=True)
                    pcm += part.inline_data.data
                    if on_audio:
                        await on_audio(part.inline_data.data)
        if sc.turn_complete:
            break
    if t_first is None:
        return None
    turn = {"lead": heard.strip(), "agent": said.strip(), "latency_ms": latency, "reply_s": round(len(pcm) / (2 * OUT_RATE), 1)}
    stats.turns.append(turn)
    return turn, bytes(pcm)


async def talk(client, args):
    stats, player, started = Stats(), Player(), now()
    await player.start()
    mic = await asyncio.create_subprocess_exec(
        "arecord", "-q", "-f", "S16_LE", "-r", str(IN_RATE), "-c", "1", "-t", "raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    last_voiced = {"t": None}

    talking = {"on": not args.ptt}

    async def pump_mic(session):
        levels, thresh, open_until, silence = [], None, 0.0, bytes(CHUNK)
        while True:
            chunk = await mic.stdout.readexactly(CHUNK)
            rms = float(np.sqrt(np.mean(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) ** 2)))
            if thresh is None:
                levels.append(rms)
                if len(levels) >= 20:  # first second = room noise
                    thresh = max(statistics.median(levels) * args.gate, 350.0)
                    print(f"  [mic ok: room noise {statistics.median(levels):.0f}, speech threshold {thresh:.0f}. Speak now.]", flush=True)
                continue
            if args.ptt:
                if not talking["on"]:
                    continue
            else:
                # noise gate: the API only hears you when you are louder than the room, so its
                # end-of-speech detection is not held open by background chatter
                if rms > thresh:
                    open_until = now() + 0.3
                    if now() > player.busy_until:
                        last_voiced["t"] = now()
                if now() > open_until:
                    chunk = silence
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={IN_RATE}"))

    async def push_to_talk(session):
        loop = asyncio.get_running_loop()
        while True:
            print("  [Enter = start talking]", flush=True)
            await loop.run_in_executor(None, sys.stdin.readline)
            await session.send_realtime_input(activity_start=types.ActivityStart())
            await player.flush()
            talking["on"] = True
            print("  [recording... Enter = done]", flush=True)
            await loop.run_in_executor(None, sys.stdin.readline)
            talking["on"] = False
            last_voiced["t"] = now()
            await session.send_realtime_input(activity_end=types.ActivityEnd())

    async def run(session):
        while True:
            res = await receive_turn(session, stats, player.write, player.flush, lambda: last_voiced["t"], live=True)
            if res:
                t = res[0]
                print(f"\nYOU  : {t['lead']}\nAGENT: {t['agent']}\n       [latency {t['latency_ms']} ms, reply {t['reply_s']} s]", flush=True)
                last_voiced["t"] = None

    print(f"Live call on {args.model}, voice {args.voice}. Headphones on. Stay quiet 1 s, then say 'Hello?'. Ctrl-C to hang up.")
    print("latency = you stop talking -> first agent audio arrives (includes the API's end-of-speech wait)")
    try:
        async with client.aio.live.connect(model=args.model, config=make_config(args)) as session:
            jobs = [pump_mic(session), run(session)] + ([push_to_talk(session)] if args.ptt else [])
            await asyncio.wait_for(asyncio.gather(*jobs), timeout=args.max_seconds)
    except asyncio.TimeoutError:
        print(f"\n[hung up after {args.max_seconds}s safety limit]")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except asyncio.IncompleteReadError:
        print("\n[microphone stream ended: is another program holding the mic?]")
    except Exception as e:  # show API errors instead of dying silently
        print(f"\n[call failed: {type(e).__name__}: {e}]")
    finally:
        if mic.returncode is None:
            mic.kill()
        try:  # wait() can block on the undrained stdout pipe; do not let hang-up hang
            await asyncio.wait_for(mic.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        await player.close()
        stats.report(args.model, started)


async def script(client, args):
    """No microphone: lead lines go in as text. Measures model + voice speed, saves the replies."""
    OUT.mkdir(exist_ok=True)
    stats, started = Stats(), now()
    async with client.aio.live.connect(model=args.model, config=make_config(args)) as session:
        for i, line in enumerate(LEAD_LINES):
            t_sent = now()
            await session.send_realtime_input(text=line)
            res = await asyncio.wait_for(receive_turn(session, stats, t_ref=lambda: t_sent), timeout=60)
            if not res:
                print(f"turn {i}: no audio returned")
                continue
            t, pcm = res
            t["lead"] = line
            save_wav(OUT / f"live_t{i}_agent.wav", pcm, OUT_RATE)
            print(f"\nLEAD : {line}\nAGENT: {t['agent']}\n       [first audio {t['latency_ms']} ms, reply {t['reply_s']} s]", flush=True)
    stats.report(args.model, started)
    print(f"listen: {OUT}/live_t*_agent.wav")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["talk", "script"])
    p.add_argument("--model", default=os.environ.get("LIVE_MODEL", "gemini-3.8-live"))
    p.add_argument("--voice", default="Kore")
    p.add_argument("--silence-ms", type=int, default=500, help="end-of-speech silence before the agent answers")
    p.add_argument("--ptt", action="store_true", help="push-to-talk with Enter: most reliable in a noisy room")
    p.add_argument("--gate", type=float, default=3.0, help="speech must be this many times louder than room noise")
    p.add_argument("--max-seconds", type=int, default=300, help="safety limit so a forgotten session cannot burn credit")
    a = p.parse_args()
    c = genai.Client(api_key=load_key(), http_options={"api_version": "v1beta"})
    try:
        asyncio.run(talk(c, a) if a.cmd == "talk" else script(c, a))
    except KeyboardInterrupt:
        pass
