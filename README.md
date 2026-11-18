# Prova

A call desk for one small business and its own customers. The owner hands over a messy customer file,
company documents and one goal ("renew the policies that expire soon"). Prova cleans the data, fills a
policy template from the documents (every value carries a source quote), proves itself in a safety test,
then calls customers in their language (Greek by default):

- three short rehearsals against a simulated version of the customer before each call
- live ranking of the next move during the call, and self-correction when the agent misspeaks
- a written confirmation link after the call (nothing is final on the phone)
- a new tactic card when a call teaches something

Everything is reachable two ways, both faces of the same REST API: a step-by-step web UI for a human, and
an MCP server for an agent that runs the whole thing in one go.

## Quick start

```bash
uv sync
echo "GEMINI_API_KEY=..." > .env        # not needed in FAKE mode
./run.sh                                # real models, http://127.0.0.1:8770
PROVA_FAKE=1 ./run.sh                   # offline: no network, no cost, full product
```

Open http://127.0.0.1:8770.

## Ready demo (no waiting)

Press **Load demo** in the top menu of any page. It loads a finished run for a fictional Greek insurance office
(Ασφαλιστικό Γραφείο Κωνσταντίνου: 68 customers, 70 calls, safety test, learned playbook) from `demo/demo.db` into the
current server, and every step of the UI is usable right away. In demo mode a call can always be started: calling
hours, attempt limits, the gap between calls and the budget are off, and identity is confirmed by name only. Consent and
"do not call" still apply. For phone calls, start with `PHONE_TUNNEL=1 ./run.sh`, open the phone link (QR on the
Calls step) on a mobile, then press Call. The same setup flow with your own files works unchanged; the demo only skips
the waiting. A demo video is in `demo/video/draft_v3.mp4`.

## The UI, step by step

1. **Data**: pick a sample (`insurance`, `gym`) or upload a .csv/.xlsx. See the column mapping, fixes, rejected rows.
2. **Company and goal**: add documents or a URL, write the goal, press Read documents.
3. **Policy**: review every slot, its source quote and status; confirm assumed ones, fill empty required ones.
4. **Playbook**: tactic cards by situation; rejected cards show the lint reason.
5. **Safety test**: run it, read each attack's transcript, then Approve and Go live.
6. **Plan**: ranked customers with reasons; customers who may not be called show the blocking rule.
7. **Calls**: start one customer or the top N, simulated (text) or on a real phone (browser phone / Twilio).
8. **Results**: report, confirmations, follow-ups, human queue, evidence packs, `results.csv`.

## One go with an agent (MCP)

`.mcp.json` registers the server as `prova` (stdio). Start `./run.sh` first, then from the agent:

```
prova_guide()                      # what this is and the normal order
run_everything(sample="insurance", n_calls=3, mode="sim")   # whole flow, returns the report
```

`run_everything` wipes the database, ingests the file, reads the documents, fills the policy, seeds the
playbook, runs the safety test, approves and goes live, ranks, calls the top `n_calls` and returns the report.
If a required slot stays empty or the safety test fails it stops and returns the blockers. Every step is
also its own tool (`load_sample`, `read_documents`, `run_safety_test`, `rank_customers`, `run_campaign`,
`report`, ...). Tools that start background work wait for the result themselves.

Over HTTP instead of stdio: `MCP_TRANSPORT=streamable-http uv run python -m prova.mcp_server` (port 8771).

Without a model or server, the same path from Python:

```bash
PROVA_FAKE=1 PROVA_DB=/tmp/prova.db uv run python -c "import asyncio; from prova import flow; \
print(asyncio.run(flow.run_all('samples/insurance/pelates.xlsx', ['samples/insurance/timokatalogos.md'], 'Renew policies', n_calls=3)))"
```

## Environment variables

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` | model key (in `.env`) |
| `PROVA_FAKE=1` | offline mode: no network, no cost |
| `PROVA_DB` | database path (default `data/prova.db`) |
| `PROVA_HOST`, `PROVA_PORT` | API bind (default 127.0.0.1:8770) |
| `PROVA_URL` | API address the MCP server talks to |
| `MCP_TRANSPORT`, `MCP_PORT` | `streamable-http` and port (default 8771) |
| `PROVA_OUT` | output folder (default `out/`: `results.csv`, `calls/*.wav`) |
| `PHONE_TUNNEL=1` | open a public tunnel for the phone page and the `/c/` confirmation links |
| `PHONE_CAP_S` | hard cap per live call in seconds (default 180) |
| `PHONE_TO` | your own number: used for test calls and test SMS |
| `PHONE_ALLOW_CUSTOMER_NUMBERS=1` | allow dialling / texting the numbers from the customer file |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | Twilio calls and confirmation SMS |
| `FIRECRAWL_API_KEY` | better web fetching and playbook research |
| `PROVA_LLM_MODEL`, `PROVA_FAST_MODEL` | model overrides |

Confirmation messages are sent by Twilio SMS only when the three `TWILIO_*` variables are set, the call was
live and `PHONE_ALLOW_CUSTOMER_NUMBERS=1`; otherwise they go to `PHONE_TO` if set, else they are only stored
and shown in the UI (and at `/c/{token}`).

## Legal note

Prova is designed around Greek and EU rules (consent for automated calls, AI disclosure, opt-out,
written confirmation, withdrawal right, GDPR access and erasure). It is not legal advice; the owner
stays responsible for how it is used. Only numbers with recorded consent are called. Phone numbers that come
from a customer file are never dialled or texted unless `PHONE_ALLOW_CUSTOMER_NUMBERS=1` is set. Erasing a
customer keeps the hash-chained event log so it stays verifiable; the erase result says what remains.

## Layout

`prova/` db, template, llm (frozen), policy, gateway, onboard, prompts, sim, evals, sidecar, voice, learn,
flow, server, mcp_server. `samples/` demo businesses. `ui/` the web UI. `tests/` pytest (FAKE mode).

## License

Copyright (c) 2026 Panagiotis Vardalas. Released under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md): you may use, change and share Prova for any
noncommercial purpose. Commercial use needs a separate license from the author; get in touch.
