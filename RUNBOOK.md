# Prova demo runbook

One line: **Small insurance agencies lose renewals because nobody has time to call every client. Prova reads
your messy files, writes its own rulebook, proves it in a sandbox, and reaches every client the lawful way.**

## Before the demo (T-30 min)

1. Fresh database, real model, tunnel for phones:
   ```bash
   rm -f data/prova.db*            # or: PROVA_DB=data/demo.db
   PHONE_TUNNEL=1 ./run.sh         # prints the public URL and the owner token
   ```
   No tunnel / client isolation on venue Wi-Fi → phone hotspot for laptop + phones.
2. Open tabs (laptop, big screen):
   - Setup: http://127.0.0.1:8770/ui/setup.html
   - Playbook: http://127.0.0.1:8770/ui/playbook.html
   - Lab: http://127.0.0.1:8770/ui/lab.html
   - Mission: http://127.0.0.1:8770/ui/mission.html
3. Have ready in one folder: `demo/konstantinou/` files (+ optional `photos/*.jpg` of the handwritten ledger
   from `printable/ledger_handwrite.html`; skip the consent form and the price-list printables).
4. **Pre-bake the learning run** (it is slow and costs money; do NOT run it live): after a full setup on a
   throwaway DB, press "Run 7 simulated days" on the Playbook page, or keep the DB from the rehearsal.
   Simplest: rehearse the whole flow once on `data/demo.db`, keep it, and present from it, re-doing only the
   setup beats live on a second DB if time allows.
5. Your phone: open the owner link `/owner` (or the `/t/...` turn-on link from the setup page) once.

## The demo (≈ 4 min)

| t | Screen | You do | You say |
|---|---|---|---|
| 0:00 | Setup | — | The one line. "This is a real 2-person agency's paperwork. Messy." |
| 0:15 | Setup | Start: company name + `https://konstantinou-asfalies.example`. Drop the xlsx, the CSV, the price list, the emails, the WhatsApp export, the photos | "No form. An agent reads everything." Point at the Case File filling, each fact with the sentence it came from |
| 0:50 | Setup → Agent Monitor | — | Red line **injection_quarantined**: the WhatsApp "give everyone 50%" line. "Documents are data, never instructions. The guard sits between the agent and every action." |
| 1:05 | Setup → questions | Answer them. Consent: **"Yes, all"** | It asks only what it can't know. Consent: "it's in our contract / privacy policy" → recorded as the owner's attestation; explicit "no" clients stay on hold; anyone else can get the ready consent link. Then the two contradicting discount emails ("which one applies? I default to the stricter"), the typo'd prices ("18O → 180?"), which detail confirms identity |
| 1:40 | Setup → goals | Pick renew (+ collect) | Goals come from the data with € at stake |
| 1:50 | Setup → Build | Build | Certificate: rules match their sources, attacks, setup attacks blocked, mutants caught. "It tests its own guardrails by breaking them." |
| 2:10 | Phone | Turn-on tap | Key rules listed in plain Greek; one tap confirms exactly that list. An agent can't do this step. |
| 2:20 | Setup → lanes | — | Most clients → AI call (consent from the contract). "Clients who said no are never called; for anyone unsure, one click sends the ready consent link." |
| 2:30 | Mission + judge phone | Judge scans the QR → Call me now | Rehearsing ×3 → browser call in Greek → judge haggles → **DENIED** above the limit → agrees → confirmation |
| 3:20 | Playbook | Switch goal tabs | Per-goal playbooks; win rate per simulated day; cards born from calls, promoted, retired |
| 3:40 | Lab | Scroll | Sandbox: attacks with transcripts, mutants, rehearsals. Labelled honestly where the rules backend graded |
| 3:55 | — | — | Close: "Your agent sets it up. Prova knows the rules, proves it follows them, and reaches every client the lawful way." |

## What to say about the numbers (honest)

- The certificate is **our own test suite**, generated per company. It proves the code enforces the rules
  it saved, and the fidelity line checks the saved rules against their source sentences. It is not a proof
  against every attack.
- The owner confirms the key rules before anything goes live.

## Fallbacks

| Breaks | Do |
|---|---|
| Real model slow / down | Restart with `PROVA_FAKE=1 ./run.sh` and present the same flow (say "offline mode") |
| Phone can't reach the laptop | Phone hotspot; or play the recorded call from the rehearsal |
| Photo misread | Skip the photos beat; the typed files carry the story |
| Live call fails | Show a finished call in Mission / Lab from the rehearsal DB |
| Anything else | The recorded backup video |
