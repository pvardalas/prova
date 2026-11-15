import base64, json, os, sys, urllib.request, concurrent.futures as cf
KEY=os.environ["GEMINI_API_KEY"]; HERE=os.path.dirname(os.path.abspath(__file__))
P={
"office":"Photorealistic wide shot of a small, cluttered independent insurance agency office in a Greek provincial city (Patras), late afternoon light, two desks, old desktop computer, ring binders, stacks of paper folders, a wall calendar, a landline phone, a window with a balcony and city street outside. No people's faces in focus, no text, no logos. 16:9 cinematic.",
"overwhelmed":"Photorealistic photo of an overwhelmed insurance office worker seen from behind/side (face not visible), at a desk buried in stacks of paper files, sticky notes, a ringing phone, a smartphone showing chat messages, a handwritten notebook, an Excel spreadsheet on the monitor. Warm muted tones, shallow depth of field, no readable text, no logos. 16:9.",
"paper":"Photorealistic close-up of a messy desk: printed price lists with handwritten corrections, a spiral notebook with handwritten notes, printed emails, a smartphone, coffee cup, sticky notes, a pen. Soft daylight, shallow depth of field, no legible text, no logos. 16:9."}
def g(k):
    for m in ["gemini-3.1-flash-image","gemini-2.5-flash-image"]:
        try:
            body={"contents":[{"parts":[{"text":P[k]}]}],"generationConfig":{"responseModalities":["IMAGE"],"imageConfig":{"aspectRatio":"16:9"}}}
            req=urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={KEY}",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
            r=json.load(urllib.request.urlopen(req,timeout=180))
            for p in r["candidates"][0]["content"]["parts"]:
                if "inlineData" in p:
                    open(os.path.join(HERE,"img",k+".png"),"wb").write(base64.b64decode(p["inlineData"]["data"])); return k,m
        except Exception as e: err=str(e)
    return k,"FAIL "+err
with cf.ThreadPoolExecutor(3) as ex:
    for r in ex.map(g,P): print(*r)
