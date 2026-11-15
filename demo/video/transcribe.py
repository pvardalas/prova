import base64, json, os, sys, urllib.request
KEY=os.environ["GEMINI_API_KEY"]
for f in sys.argv[1:]:
    b=base64.b64encode(open(f,"rb").read()).decode()
    body={"contents":[{"parts":[{"inlineData":{"mimeType":"audio/wav","data":b}},{"text":"Transcribe this audio verbatim, with approximate timestamps for each sentence."}]}]}
    req=urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={KEY}",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    r=json.load(urllib.request.urlopen(req,timeout=120))
    print(f, r["candidates"][0]["content"]["parts"][0]["text"])
