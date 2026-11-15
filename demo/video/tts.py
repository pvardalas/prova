# Generate narration WAVs with Gemini TTS. Usage: python tts.py [ids...]
import base64, json, os, sys, wave, urllib.request, concurrent.futures as cf
HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ["GEMINI_API_KEY"]
MODELS = [os.environ.get("TTS_MODEL", "gemini-3.8-flash-tts"), "gemini-2.5-pro-preview-tts", "gemini-2.5-flash-preview-tts"]
VOICE = os.environ.get("TTS_VOICE", "Charon")
STYLE = os.environ.get("TTS_STYLE", "")
segs = json.load(open(os.path.join(HERE, "segments.json")))
want = set(sys.argv[1:])
def gen(s):
    out = os.path.join(HERE, "audio", s["id"] + ".wav")
    if os.path.exists(out) and not want: return s["id"], "cached"
    err = None
    for m in MODELS:
        body = {"contents": [{"parts": [{"text": STYLE + s["text"]}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                  "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
        for attempt in range(2):
            try:
                req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={KEY}",
                        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                r = json.load(urllib.request.urlopen(req, timeout=120))
                pcm = base64.b64decode(r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                with wave.open(out, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
                return s["id"], m
            except Exception as e:
                err = f"{m}: {e}"
    return s["id"], "FAIL " + str(err)
todo = [s for s in segs if not want or s["id"] in want]
with cf.ThreadPoolExecutor(6) as ex:
    for r in ex.map(gen, todo): print(*r)
