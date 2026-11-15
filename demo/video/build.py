"""Assemble the Prova demo video from HTML/CSS scenes + Gemini TTS narration.

  ./build.sh                    -> draft_v1.mp4
  ./build.sh --out v2.mp4       -> other name
  ./build.sh --only d1,d6       -> re-record only these scenes (others reuse clips/*.webm)
Each segment in segments.json = narration audio/<id>.wav + scene scenes/<scene>.html.
The scene is recorded with Playwright (1920x1080) for exactly the narration length.
Env: TEMPO (narration speed-up, default 1.08), JOBS (parallel recordings, default 4).
"""
import asyncio, json, os, subprocess, sys, time
import imageio_ffmpeg

HERE = os.path.dirname(os.path.abspath(__file__))
FF = imageio_ffmpeg.get_ffmpeg_exe()
TEMPO = float(os.environ.get("TEMPO", "1.08"))
JOBS = int(os.environ.get("JOBS", "4"))
LEAD, TAIL = 0.3, 0.35
args = sys.argv[1:]
OUT = args[args.index("--out") + 1] if "--out" in args else "draft_v1.mp4"
ONLY = set(args[args.index("--only") + 1].split(",")) if "--only" in args else None


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-2500:]); sys.exit("ffmpeg failed")
    return r


def dur(p):
    r = subprocess.run([FF, "-i", p], capture_output=True, text=True).stderr
    h, m, s = r.split("Duration: ")[1].split(",")[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def scene_url(s):
    name = s.get("scene", s["id"])
    q = f"?d={s['dur']:.2f}" + (f"&hl={s['hl']}" if "hl" in s else "")
    return "file://" + os.path.join(HERE, "scenes", name + ".html") + q


async def record(browser, s, sem):
    from playwright.async_api import Error
    async with sem:
        vdir = os.path.join(HERE, "clips", "raw_" + s["id"])
        os.makedirs(vdir, exist_ok=True)
        for f in os.listdir(vdir):
            os.remove(os.path.join(vdir, f))
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=vdir,
                                        record_video_size={"width": 1920, "height": 1080})
        t0 = time.monotonic()
        page = await ctx.new_page()
        await page.goto(scene_url(s), wait_until="load")
        await page.wait_for_timeout(600)  # fonts/images settle
        start = time.monotonic() - t0
        await page.evaluate("window.go()")
        await page.wait_for_timeout(int(s["dur"] * 1000) + 400)
        await ctx.close()
        raw = os.path.join(vdir, os.listdir(vdir)[0])
        s["_raw"], s["_ss"] = raw, start
        print(f"  recorded {s['id']} ({s['dur']:.1f}s, offset {start:.2f}s)", flush=True)


async def record_all(segs):
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(JOBS)
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--force-device-scale-factor=1"])
        await asyncio.gather(*(record(b, s, sem) for s in segs))
        await b.close()


def main():
    segs = json.load(open(os.path.join(HERE, "segments.json")))
    for sub in ("clips", "audio_proc"):
        os.makedirs(os.path.join(HERE, sub), exist_ok=True)
    trim = "silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse"
    for s in segs:
        a_out = os.path.join(HERE, "audio_proc", s["id"] + ".wav")
        run([FF, "-y", "-i", os.path.join(HERE, "audio", s["id"] + ".wav"), "-af",
             f"{trim},atempo={TEMPO},adelay={int(LEAD * 1000)},apad=pad_dur={TAIL},aresample=48000", "-ac", "2", a_out])
        s["dur"], s["_a"] = dur(a_out), a_out
    todo = [s for s in segs if ONLY is None or s["id"] in ONLY]
    print(f"recording {len(todo)} scenes ...", flush=True)
    asyncio.run(record_all(todo))
    meta_p = os.path.join(HERE, "clips", "meta.json")
    meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
    for s in todo:
        meta[s["id"]] = {"raw": s["_raw"], "ss": s["_ss"]}
    json.dump(meta, open(meta_p, "w"), indent=1)
    parts = []
    for s in segs:
        m = meta[s["id"]]
        clip = os.path.join(HERE, "clips", s["id"] + ".mp4")
        d = s["dur"]
        vf = f"fps=30,scale=1920:1080,setsar=1,tpad=stop_mode=clone:stop_duration=2,trim=duration={d:.3f},fade=in:st=0:d=0.25,fade=out:st={d - 0.25:.3f}:d=0.25,format=yuv420p"
        run([FF, "-y", "-ss", f"{m['ss']:.3f}", "-i", m["raw"], "-i", s["_a"], "-filter_complex", f"[0:v]{vf}[v]",
             "-map", "[v]", "-map", "1:a", "-t", f"{d:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", clip])
        parts.append(clip)
    lst = os.path.join(HERE, "clips", "list.txt")
    open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    out = os.path.join(HERE, OUT)
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out])
    t = 0
    for s in segs:
        print(f"{s['id']:4} {t:6.1f}s  +{s['dur']:5.2f}s"); t += s["dur"]
    print(f"\n{out}  {dur(out):.1f}s")


if __name__ == "__main__":
    main()
