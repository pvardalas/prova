# BUILD.md: build orders (read this first, then the files it points to)

Design: `PLAN.md` (v3). Implementation: `IMPLEMENTATION.md` (sections 1–6, overridden by 7 and 8).
Security: `docs/agent_guard.md`. Law: `docs/law_sources.md`. Base rules: `SPEC.md` section 1.
Baseline: `PROVA_FAKE=1 uv run pytest -q` → 187 passed. It must stay green after every change you make.

## 1. Reviewer decisions (these override the plans)

**R1 Scope cut for the build (MVP).** Build only what the demo path needs:
- IN: setup agent + setup gateway, OCR of a customer-list photo, compliance lanes + brief + consent page,
  web-only browser call from the consent page, rules (new slots, DSL, call hours, must_say, per-language
  disclosure), radar + suggest_goal + brief, generated attacks + setup attacks + mutation check, Agent Guard
  core (registry, check pipeline, scan heuristic, egress fetch, limits, classifier with FAKE rules + fast-tier
  backend), security fixes, UI (setup chat + Case File + Agent Monitor, mission, consent, owner, inbox).
- OUT for now: proof file (WP-I), consent-form OCR, synthetic population, `record_needs`, local ONNX classifier
  (keep the backend interface only), MCP elicitation (return questions instead), live "name any business",
  morning_run scheduler UI beyond one button, Twilio/SMS anything.

**R2 Channels are web-only.** Do not touch `voice._twilio_number`, `flow._sms_target`, `_send_sms`. No code
path may reach them from new code. The IMPLEMENTATION §3 WP-D dial change is dropped.

**R3 New slots must not block existing flows.** `call_hours` is `required: False`; `effective()` fills it from
`law.pack(country)["call_hours"]` when empty. `country` is `required: True`, and the FAKE fill must infer it
(phone prefixes +30 / site address → "GR"). `consent_basis` and `human_calls_allowed` are `required: False`
(they drive lanes and questions, not `missing_required`). Every existing test must still pass.

**R4 Law data comes only from `docs/law_sources.md`.** `prova/law.py` items carry `source_id` and a `quote`
copied character for character from that file; a test asserts each quote is a substring of
`docs/law_sources.md`. Items whose status there is S-only or X get `verified: False` and must never make a lane
or action *allowed*. GR human-call lane: off (`human_calls: "disabled_until_source"`). Never write law text from memory.

**R5 Where the guard sits.** MCP server stays a thin HTTP client; identity, scopes and the guard pipeline are
enforced in the **REST layer** (`server.py` dependency) and inside Python call sites (`gateway.CallCtx.run_tool`
calls `guard.check` before `policy.check_action`; setup agent tools call `guard.check`). Local UI on 127.0.0.1
without a token = owner scope; MCP-over-HTTP and tunnel requests need a bearer token (`PROVA_TOKEN`, printed at
start if generated). The classifier never allows what code denies.

**R6 Browser call bound to the customer.** The consent page's "Call me now" opens a browser call **for that
customer only**: the phone WebSocket gets the consent token, the server binds the next call for that customer to
that socket (no `_idle_phone()` guessing). FAKE mode: `_fake_call` path as today.

**R7 No real model or network spend.** Build and test in FAKE mode only. Every new model call has a realistic
`fake=`. The reviewer does the real-model pass.

**R8 Definition of done** = `tests/test_demo_path.py` (FAKE) passes, plus the full suite:
start setup (company + `https://papadakis-asfalies.example`) → site resolves to `samples/insurance/site.md`
→ add `pelates.xlsx` → add the list photo truth (OCR fake) → ≤ 3 questions, consent + identity among them → a
planted instruction in the site/terms is **denied** and logged as `agent_action` → answer the questions → build →
certification (attacks, generated, setup attacks, mutants all caught) → turn-on token → owner tap → live → lanes
computed → consent page for one consent_request customer → "call now" → consent recorded → browser call (fake) →
won → confirmation page. Asserts: no call to `_send_sms` / Twilio; dangerous MCP tools gone or gated.

## 2. Builders, files, order

Do not edit files outside your list. If you need a change in someone else's file, write it under
"Requests" in your final report. Never `git commit`. Never edit `BUILD.md`, `PLAN.md`, `IMPLEMENTATION.md`, `docs/`.

| Builder | Phase | Owns (may edit / create) |
|---|---|---|
| **F (foundation)** | 1, first | `prova/db.py`, `prova/template.py`, `prova/llm.py`, `prova/law.py`, `packs/`, `prova/guard/*` (new), `prova/mcp_server.py`, `prova/server.py` (auth dependency + security fixes only), `prova/onboard.py` (`add_url` → `guard.net.fetch` + sample-domain resolver only), `prova/learn.py` (card scan hook only), tests for these |
| **U (UI + samples)** | 1, parallel with F | `ui/*` (new pages; `index.html` nav only), `samples/*`, `samples/make_samples.py` |
| **A (setup)** | 2, after F | `prova/setup_agent.py`, `prova/intake.py`, `prova/goals.py`, `prova/flow.py`, `prova/onboard.py` (fill_policy / seed_playbook hooks, new-slot filling and fakes; not add_url), tests |
| **B (rules + compliance + evals)** | 2, after F | `prova/policy.py`, `prova/gateway.py`, `prova/prompts.py`, `prova/compliance.py`, `prova/evals.py`, `prova/sim.py`, `prova/sandbox.py`, `prova/voice.py` (R6 binding only), tests |
| **U again** | 3, after A + B | `prova/server.py` (new route block), `prova/mcp_server.py` (new tools), wire UI, `tests/test_demo_path.py` |

Phase 1 F must land the **contract stubs** below (real signatures, safe defaults, docstrings) so A, B, U code
against them. A and B then replace the stubs in their own files.

## 3. Contracts F creates as stubs

```python
# prova/policy.py (F adds stubs at the end; B owns and implements)
def match(cond, customer, now=None) -> bool: return False
@contextmanager
def overridden(slots: dict): yield
def call_language(customer: dict) -> str: return "el"

# prova/compliance.py (F creates stub; B implements)
LANES = ("ai_call", "consent_request", "human_call", "hold")
def lane(customer: dict) -> dict: return {"lane": "hold", "rule": "stub", "reason": "not implemented", "next": "none"}
def lanes_summary(customers=None) -> dict
def gaps() -> list[dict]
def brief() -> dict
def consent_page(token: str) -> dict
def consent_answer(token: str, choice: str, when: str | None = None) -> dict
def join(name: str, phone: str, language: str, consent_text_version: str) -> dict
def send_consent_requests(customer_ids=None) -> dict      # web inbox links only

# prova/setup_agent.py (F creates stub; A implements)
def start(company, website=None, description="", face="chat", agent=None) -> dict
def add(path=None, url=None) -> dict
def case_file() -> dict
def questions() -> list[dict]
def answer(qid, answer, by="owner") -> dict
def skip(qid) -> dict
async def build() -> dict
def review() -> dict
def request_turn_on() -> dict
def turn_on(token: str) -> dict
async def chat(message: str) -> dict

# prova/goals.py (F creates stub; A implements)
def suggest_goal() -> dict
def radar(goal=None, days=45) -> dict
def brief() -> dict

# prova/guard/check.py (F implements)
def check(actor: dict, tool: str, args: dict, ctx: dict | None = None) -> dict
    # -> {"decision": "allow"|"deny"|"escalate", "rule": str, "hint": str, "risk": float}
```

Question object and lane object shapes: `IMPLEMENTATION.md` section 2. Tool registry entry shape:
`docs/agent_guard.md` section 2. Every existing tool (gateway TOOLS, MCP tools) gets a registry entry.

## 4. Final report (every builder)

What works (command + real output tail), what does not, deviations from this file, requests for other owners,
dependencies needed (do not edit `pyproject.toml`). No padding.
