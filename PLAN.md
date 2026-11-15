# Prova: final plan v3 (compliance-first, agentic setup)

## 1. What it is

**Insurance (the fit):** small agencies lose renewals because nobody has time to call every client. Prova
calls every expiring policy, in the client's language, before it lapses, only where the law allows, and
files the proof.

**Any business (the engine):** tell Prova (or let your agent tell it) your company name and website. It
works out which rules apply to you, asks you at most a handful of questions, writes and proves its own
rulebook, then reaches every customer through the lawful channel for them.

```
IN   company name + website   (+ anything else you have: files, customer list, terms, scripts)
     via chat in the Prova UI, or via MCP from your own agent
OUT  Case File: what Prova knows about your business and how it knows it
     Compliance Brief: the laws that apply, what Prova enforces, what stays yours to do
     Rulebook + certification (generated tests, mutation check)
     Lanes: every customer → AI call / consent request / human call / hold
     Then: calls, written confirmations, signed proof per call, morning brief
```

Three principles, used everywhere:

1. **Model writes, code enforces, tests prove.** A rule the code cannot enforce is *soft* and labeled. A rule
   without a passing test is not active. The legal floor is data, never generated, can only be tightened.
2. **Every fact has a provenance.** `verified` (in the data) · `sourced` (quoted from a document or the site)
   · `attested` (the owner or their agent said so, recorded) · `assumed` (safe default). Anything that needs
   a legal basis (calling, recording, selling) needs `verified` or `attested`. `assumed` always picks the
   safest lane.
3. **Nobody is a dead end, nothing is skipped silently.** Every customer gets a lane with a next action;
   every unanswered question has a safe default; setup always completes.

## 2. Setup: one interview, two faces

### 2.1 Flow (identical for chat and MCP)

```
 1 READ      crawl website, read files, map customer columns                  (no questions)
 2 INFER     Case File facts with provenance: type, country, languages,
             products/prices, contact human, consent data, recording, ...       (no questions)
 3 GAP       code computes: facts REQUIRED by (floor pack × industry pack ×
             goal × channel) minus facts known → gap list, ranked by impact
             (€ at stake × customers whose lane changes)
 4 ASK       only gaps that block or change a lane. Budget: 5, target 3.
             Each question: why (one line + law ref + link), options, what
             happens if skipped (safe default)
 5 COMPILE   rulebook + lanes + Compliance Brief from facts + packs
 6 CERTIFY   sandbox, generated tests, mutation check
 7 TURN ON   ONE human tap (SMS/email link to the owner). An agent can do
             everything except this; attestations by an agent are recorded
             as "by agent X on behalf of owner Y" and confirmed by the tap
```

**Minimal by construction:** the setup agent can only ask a question whose fact key is in the code-computed
gap list. It cannot wander, and the UI shows "inferred 41 facts · asked 3".

### 2.2 The questions (insurance demo, what a real run should produce)

| # | Gap found | Question (with the why) | If skipped |
|---|---|---|---|
| 1 | customer file has no consent column | "I don't see call consent in your client list. In Greece, automated (AI) calls need prior consent (law 3471/2006 art. 11). Do you have it? **All · Some (upload list or the form) · No / not sure**" | everyone goes to consent-request or human lane |
| 1b | owner says "All" | "Where was it given? Upload your application form and I'll check the consent clause covers automated calls." | recorded as attested, clause unchecked (shown in brief) |
| 2 | no discount limit on site or files | "Highest discount I may give on a renewal?" | 0% (list price only) |
| 3 | handoff person unclear | "Who takes over when a client wants a person? (I found 210 xxx on your site; is that right?)" | site number, marked assumed |

Things Prova does NOT ask, because it decides safely by itself: recording (on, with notice), call hours
(floor-pack defaults), AI disclosure (always), confirmation channel (SMS link), language (per customer).
All editable later in the Case File.

### 2.3 Chat face (Prova UI)

- Left: chat with the setup agent. Questions arrive as quick-reply chips; free text accepted.
- Right: the **Case File** building live while it reads: facts with provenance badges
  (verified / sourced with the quote / attested / assumed), lanes forming, rule count, test count.
- Bottom: Compliance Brief preview and the "Turn on" step (tap sent to owner's phone).

### 2.4 MCP face (set it up with your agent)

Same session, same state, so a human can switch between faces at any point.

```
setup_start(company, website, description=None) -> {session, inferred: {...}, questions: [...]}
setup_add(session, file | url)                  -> {new_facts, questions}      # questions may shrink
setup_answer(session, question_id, answer, attested_by)  -> {facts, questions, lanes}
setup_status(session)                           -> Case File summary + gaps + progress
setup_review(session)                           -> rulebook diff + Compliance Brief + certification
setup_request_turn_on(session)                  -> sends the owner the one-tap link; returns pending
```
Questions are structured `{id, fact, text, why, law_ref, url, options, default_if_skipped}` so the
calling agent can answer from its own context (it may already have the CRM or the forms) or ask its human.
When the MCP client supports elicitation, the server asks the human directly through the client; otherwise
the questions are returned in the tool result.

Runtime tools stay as now (`find_due`, `run_campaign`, `morning_run`, `get_brief`, `get_proof`, ...).

### 2.5 The setup agent itself
An LLM loop with the same internal tools as the MCP surface (crawl, read_file, map_columns, infer_facts,
compute_gaps, ask, compile, certify). The backbone is code: the fact checklist, gap computation, question
budget and turn-on gate. FAKE mode: scripted agent decisions built from the real inputs.

## 3. Compliance stack (what runs where)

```
 LAW         Floor packs (EU base, GR overlay; data, versioned)       never generated
 BUSINESS    Industry pack (insurance curated; others inferred)       priors, not rules
 FACTS       Case File with provenance + owner attestations           who said what, when
 RULES       compiled rule primitives over one condition DSL          model writes, code checks
 ENFORCE     may_dial (pre-dial) · gateway (in-call tools) ·          code
             check_agent_text + sidecar (in-call speech) · close_call (post-call)
 PROVE       generated tests + red team + mutation check → certification
 EVIDENCE    hash-chained events, signed proof file per call, consent records
 REPORT      Compliance Brief, morning brief, audit export, GDPR access/erasure
 CHANGE      new doc / site change / answer → recompile → rerun affected tests → re-certify
```

### 3.1 Lanes (assigned by code from floor pack + facts)

| Lane | Who | Prova does |
|---|---|---|
| **AI call** | automated-call consent verified or attested | calls; AI disclosure + recording notice first (AI Act art. 50; GDPR notice) |
| **Consent request** | existing client, no call consent, lawful written channel (existing-customer SMS/email for similar products, opt-out in every message) | short notice + **Call me now / Pick a time / Stop**; a tap records consent (text shown, time, hash) and moves them to AI call |
| **Human call** | no automated-call consent; human marketing calls allowed (number not on the opt-out register: GR art. 11 register, owner checks) | Prova prepares the brief (rehearsed, offers, objections); staff call; result logged the same way |
| **Hold** | opted out, do_not_call, no lawful channel | no contact; reason shown; counted in the brief |

Not offered, by design: AI calls without consent. A disclosure does not replace consent. The Compliance
Brief explains this to the owner in one paragraph, with the alternative lanes.

### 3.2 Compliance Brief (generated, refreshed on every change)
- Laws that apply (country × industry × channel). For each: what it requires · what Prova enforces · what
  stays the owner's job · source link. Insurance adds IDD items (demands and needs before a product is
  agreed, product information, record-keeping) and distance-selling withdrawal.
- Coverage: customers and € per lane; "128 clients lack call consent → consent requests ready to send".
- Attestations on record and which are unchecked (e.g. consent clause not uploaded).
- Owner to-dos, short: opt-out register check, DPIA for AI calls, retention period.
- Floor pack version, certification result. Labeled "not legal advice; confirm with counsel".

### 3.3 Rule primitives and DSL (unchanged from v2)
One condition DSL (`all/any/none` of `{field, op, value}`, ops `== != < <= > >= in not_in exists
days_until<= days_since>=`) for rules, goals, radar, lanes, eligibility, tests. Primitives, each enforced at
one point with a test template: `limit_number, eligibility, require_before, field_visibility, must_say,
forbid_claim, dial_window, dial_limit, segment_block, escalate_on, confirm_channel`, plus new `lane_rule`
(condition → lane) used by `compliance.py`.

### 3.4 Language (unchanged from v2)
Per customer: explicit column → last call → phone country code → company default; never from the name.
Disclosure lines generated per language, back-translated, stored, checked against the stored line.
Tier A (el, en) numbers checked by code; tier B model-verified and labeled.

### 3.5 Tests (unchanged from v2)
Per-rule templates + floor tests + generated red team (code-checked) + mutation check. New: lane tests
(a no-consent customer is never AI-dialled in any mode except sandbox; a consent tap moves the lane).

## 4. Data model (WP0, reviewer-owned)

> Target design. For the build on the current code, IMPLEMENTATION.md collapses most of these into existing
> tables (policy_slot, customer, settings, handoff) and adds only `consent_request` + 2 event types.

New tables:
- `fact(key, value, provenance, source, by, at)`: the Case File
- `question(id, fact_key, text, why, law_ref, url, options, default, status, answer, answered_by, at)`
- `rule(id, primitive, params, cond, source_quote, status, test_ids)`
- `goal(id, name, segment, success_tool, value_field, metric, status)`
- `profile(customer_id, language, lane, lane_rule, value, notes)`
- `consent(customer_id, kind, text_shown, channel, given_at, hash, withdrawn_at)`
- `test_case(id, rule_id, kind, script, check, last_result)`
- `setup_session(id, face: chat|mcp, agent_id, status, turn_on_token, turned_on_by, at)`

New event types: `fact`, `question`, `attestation`, `consent`, `compile`, `test`, `mutation`, `turn_on`.
Files: `floors/eu.json`, `floors/gr.json`, `packs/insurance.json`, `template.PRIMITIVES`.

## 5. Work packages

| WP | Files | What | Priority |
|---|---|---|---|
| **0** | `db.py`, `template.py`, `floors/`, `packs/` (reviewer) | section 4 | MUST, first |
| **1** | `prova/setup_agent.py` (new), `onboard.py` hooks | read, infer facts with provenance, gap computation, question budget, answer handling, attestation, turn-on token | MUST |
| **2** | `prova/compile.py` (new) | facts + packs → rules (DSL/primitives, validator, conflicts, soft), recompile diff | MUST |
| **3** | `prova/compliance.py` (new) | lanes, consent request + tap record, human-call queue with brief, Compliance Brief | MUST |
| **4** | `policy.py`, `gateway.py` | enforce `rule` rows + lanes at their points; DSL evaluator; per-language disclosure check | MUST |
| **5** | `prova/sandbox.py` (new), `evals.py`, `sim.py` | synthetic population, personas, generated tests, red team, mutation check, lane tests, certification | MUST |
| **6** | `prova/goals.py` (new), `flow.py` | goal proposals, radar with lanes, profiles + language, morning run, brief | MUST |
| **7** | `server.py`, `mcp_server.py` | setup tools (2.4) with elicitation fallback, runtime tools, routes | MUST |
| **8** | `ui/index.html`, `ui/setup.html`, `ui/mission.html`, `ui/join.html` | chat + live Case File, lanes radar, mission control, `/join` = consent-request page | MUST |
| **9** | `prova/proof.py` (new) | signed proof file per call, fields from pack | SHOULD |
| **10** | `samples/` | insurance file without a consent column (forces question 1), application form with a consent clause, fictional agency site | MUST |

Order: WP0 → WP1–6 in parallel against WP0 contracts → WP7 → WP8 → WP9/10 → dress rehearsals.
Cut first if late: WP9, then live "name any business", then mutation check UI (keep the number).

`/join` dial gate: a file number is dialled only after a recorded consent tap (`consent` row), for any
customer. This replaces the `PROVA_DEMO_JOIN` flag: the demo path and the production path are the same code.

## 6. Demo (3 minutes, split screen: owner's agent left, Prova right)

| t | Beat |
|---|---|
| 0:00 | One line (insurance). |
| 0:10 | Left, Claude: "Set up Prova for my agency, papadakis-asfalies.example, files in this folder." Right: Case File fills live from the site and files. "Inferred 41 facts." |
| 0:35 | Prova returns 3 questions. Claude answers 2 from the files it has (discount from the terms, handoff from the site). Asks the human one: **"No consent column in your client list. AI calls in Greece need prior consent. Do you have it?"** Presenter: "Some." |
| 0:50 | Lanes form: 9 AI call · 19 consent request · 2 human · 1 hold. Compliance Brief one click. Certified (tests + mutants caught). Owner's phone: **Turn on** → tap. Live. |
| 1:10 | "Send consent requests." Judge scans the QR (= the consent page they would get by SMS) → **Call me now** → rehearsal ×3 → phone rings. |
| 1:25 | Judge haggles, asks 40% → **DENIED** → agrees → SMS → tap → **RENEWED** → proof file. |
| 2:20 | Presenter tells Claude "discount is 5% now". Prova recompiles, reruns affected tests, re-certifies. |
| 2:35 | "Name any business" → setup again, different country/industry → different laws in the brief (fallback: prepared gym run). |
| 2:55 | Close: "Your agent sets it up. Prova knows the rules, proves it follows them, and reaches every customer the lawful way." |

## 7. Risks

| Risk | Answer |
|---|---|
| Wrong extracted fact or rule | provenance + quote on every item; owner review; tests + mutants |
| Agent attests something false | recorded as attestation by agent for owner; turn-on needs the owner's tap; unchecked attestations listed in the brief |
| Rule relaxes the law | floor is code + data; rules only tighten; rejected with reason |
| Lane conditions vary by country | floor packs per country, versioned; unknown country → strictest; counsel items listed |
| Profiling / GDPR | business data only, explainable, no sensitive traits; access/erasure exists |
| Setup asks too much | code gap list + budget 5; skip always allowed with safe default |
| Scope | WP0 contracts first; FAKE mode per WP; cut order in section 5 |
| Service must not stall | lanes; channel fallback Twilio → browser phone → consent SMS → human queue; model fallback to fast tier; jobs resumable from the event log |
| Twilio / venue | upgraded account, venue test, browser-phone fallback, recorded best run |
| Legal claims on stage | references shown as "applies, confirm with counsel"; no statistics without a source |
