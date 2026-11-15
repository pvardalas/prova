"""Compliance lanes: who may be reached, how, and why. Pure code over the customer file, the policy and prova/law.py.

Every customer is on exactly one lane, computed (never stored):
  ai_call          consent for automated calls on record (a tap, or file consent the owner's answers do not void)
  consent_request  existing customer we may ask for consent through a web link (no SMS: web inbox / QR only)
  human_call       staff may phone them (OFF unless the law pack allows it AND the owner said yes)
  hold             nobody contacts them; the rule says why

Law items that are not verified (prova/law.py `verified: False`) never make a lane or an action allowed.
Not legal advice.
"""
import hashlib
import re
import secrets
from datetime import timedelta

from prova import db, law, policy

LANES = ("ai_call", "consent_request", "human_call", "hold")
CONSENT_VERSION = "2026-11-v1"
TOKEN_DAYS = 14
_REQUEST_LAW_ITEM = "eu-eprivacy-13-2"
_AUTOMATED_WORDS = re.compile(r"αυτοματ|automat|ψηφιακ|τεχνητ|artificial|\bai\b|robot|ρομποτ|assistant")
_CONSENT_WORDS = re.compile(r"συναιν|συγκατ|consent")
_PHONE_WORDS = re.compile(r"τηλεφων|telephone|phone|call")


# ---------------------------------------------------------------- helpers

def _s() -> dict:
    return policy.effective()


def _pack(s: dict | None = None) -> dict:
    return law.pack((s or _s()).get("country"))


def _customers(customers=None) -> list[dict]:
    return customers if customers is not None else db.q("SELECT * FROM customer ORDER BY id")


def _value(c: dict) -> float:
    f = c.get("fields") or {}
    return float(policy.to_float(f.get("price"), 0.0) or policy.to_float(f.get("amount_due"), 0.0) or 0.0)


def _company() -> str:
    return _s().get("company_name") or (db.company() or {}).get("name") or "the company"


def _law_verified(pk: dict, item_id: str) -> bool:
    return any(i["id"] == item_id and i["verified"] for i in pk["laws"])


def _res(lane_, rule, reason, nxt):
    return {"lane": lane_, "rule": rule, "reason": reason, "next": nxt}


def _flags(c: dict) -> list:
    return c.get("flags") if isinstance(c.get("flags"), list) else []


# ---------------------------------------------------------------- lanes

def lane(customer: dict) -> dict:
    """{lane, rule, reason, next: call|send_request|queue_human|none} for one customer. Never raises (error -> hold)."""
    try:
        return _lane(customer or {}, _s())
    except Exception as e:  # noqa: BLE001
        return _res("hold", "error", f"could not decide ({type(e).__name__})", "none")


def _lane(c: dict, s: dict) -> dict:
    f = c.get("fields") or {}
    consent = c.get("consent") or {}
    tap = str(consent.get("source") or "").startswith("tap") and consent.get("automated_calls") is True
    if c.get("status") == "do_not_call" or f.get("do_not_call") is True:
        return _res("hold", "do_not_call", "asked not to be contacted", "none")
    if c.get("status") in ("won", "lost"):
        return _res("hold", "customer_status", f"already {c['status']}", "none")
    if c.get("status") == "pending_confirm":
        return _res("hold", "pending_confirm", "an agreement waits for the customer's confirmation", "none")
    for seg in s.get("blocked_segments") or []:
        if policy.match(seg, c):
            return _res("hold", "segment_block", "in a segment the company put on hold", "none")
    if consent.get("automated_calls") is False and not tap:
        return _res("hold", "consent_refused", "no consent: the customer refused automated calls", "none")
    if f.get("opt_out_register") is True and not tap:
        return _res("hold", "opt_out_register", "number is on the do-not-call register", "none")
    co = policy.consent_ok(c, s)
    if co["ok"]:
        return _res("ai_call", f"consent_{co['how']}", co["reason"], "call")
    pk = _pack(s)
    has_contact = bool(c.get("phone") or f.get("phone") or f.get("email"))
    channels = pk["lanes"].get("consent_request_channels") or []
    if has_contact and "web" in channels and _law_verified(pk, _REQUEST_LAW_ITEM):
        why = co["reason"] if consent.get("automated_calls") is True else "no consent for automated calls: ask by web link"
        return _res("consent_request", _REQUEST_LAW_ITEM, why, "send_request")
    human = pk["lanes"].get("human_calls")
    if human in ("allowed", "allowed_unless_opt_out") and s.get("human_calls_allowed") is True and f.get("opt_out_register") is not True:
        return _res("human_call", "human_calls_allowed", "staff may phone; no automated-call consent", "queue_human")
    return _res("hold", "no_lawful_channel", "no lawful channel for this customer", "none")


def lanes_summary(customers=None) -> dict:
    cs = _customers(customers)
    counts, eur, rules, ids = {k: 0 for k in LANES}, {k: 0.0 for k in LANES}, {}, {k: [] for k in LANES}
    for c in cs:
        r = lane(c)
        counts[r["lane"]] += 1
        eur[r["lane"]] += _value(c)
        rules.setdefault(r["lane"], {})
        rules[r["lane"]][r["rule"]] = rules[r["lane"]].get(r["rule"], 0) + 1
        ids[r["lane"]].append(c["id"])
    return {"total": len(cs), "counts": counts, "eur": {k: round(v, 2) for k, v in eur.items()}, "rules": rules, "customer_ids": ids}


# ---------------------------------------------------------------- gaps (questions the owner must answer)

def _clause_gap() -> str | None:
    """A document sentence that gives consent for telephone contact without saying automated / AI calls."""
    for d in db.q("SELECT name, text FROM doc"):
        for sent in re.split(r"[.\n!?;]+", str(d.get("text") or "")):
            n = policy.norm(sent)
            if _CONSENT_WORDS.search(n) and _PHONE_WORDS.search(n) and not _AUTOMATED_WORDS.search(n):
                return f"{d['name']}: {sent.strip()[:160]}"
    return None


def _gap(id_, fact, text, why, law_ref, options, default, customers, eur, free_text=True):
    return {"id": id_, "fact": fact, "text": text, "why": why, "law_ref": law_ref, "options": options, "free_text": free_text,
            "default_if_skipped": default, "impact": {"customers": customers, "eur": round(eur, 2)}}


def gaps() -> list[dict]:
    """Compliance questions found by code in the data. Shape: IMPLEMENTATION section 2 question object."""
    s, cs, out = _s(), _customers(), []
    pk = _pack(s)
    basis = s.get("consent_basis") if isinstance(s.get("consent_basis"), dict) else {}
    ref = "gr-3471-11" if pk["country"] == "GR" else "eu-eprivacy-13"
    marks = [c for c in cs if (c.get("consent") or {}).get("automated_calls") is True and policy.MARK_FLAG in _flags(c)
             and not str((c.get("consent") or {}).get("source") or "").startswith("tap")]
    blank = [c for c in cs if (c.get("consent") or {}).get("automated_calls") is None]
    clause = _clause_gap() if cs else None
    unknown = basis.get("automated_calls") in (None, "unknown")
    if cs and unknown and (blank or marks or (clause and basis.get("covers_automated") is None)):
        at_risk = {c["id"]: c for c in blank + marks}
        n_yes = sum(1 for c in cs if (c.get("consent") or {}).get("automated_calls") is True) - len(marks)
        parts = [f"{n_yes} clients have a clear yes", f"{len(marks)} only a mark (x)", f"{len(blank)} nothing"]
        text = (f"Automated (AI) calls need the client's prior consent. In your file: {', '.join(parts)}. "
                "Do your clients have that consent for automated calls?")
        why = "Without recorded consent nobody is called by the AI; they go to a consent request instead."
        if clause:
            why += f" Your terms say: \"{clause}\": that names telephone contact, not automated calls."
        out.append(_gap("q_consent", "consent_basis", text, why, ref,
                        [{"id": "all", "label": "Yes, all of them agreed to automated calls"},
                         {"id": "some", "label": "Some did (I will upload the list or the form)"},
                         {"id": "none", "label": "No, or I am not sure"}],
                        {"value": {"automated_calls": "unknown"}, "effect": f"{len(at_risk)} clients go to consent request"},
                        len(at_risk), sum(_value(c) for c in at_risk.values())))
    if cs and marks and not unknown and basis.get("automated_calls") != "none" and basis.get("marks_mean_yes") is None:
        out.append(_gap("q_consent_marks", "consent_basis",
                        f"{len(marks)} clients have only an x or a tick in the consent column. Does that mean yes?",
                        "A mark is counted as consent only if you confirm it means yes; otherwise they are asked by link.", ref,
                        [{"id": "marks_yes", "label": "Yes, a mark means they agreed"}, {"id": "marks_no", "label": "No, treat them as not asked"}],
                        {"value": {"marks_mean_yes": False}, "effect": f"{len(marks)} clients go to consent request"},
                        len(marks), sum(_value(c) for c in marks)))
    idc = s.get("identity_check")
    if cs and isinstance(idc, dict):
        field = idc.get("field")
        have = sum(1 for c in cs if policy._field(c, field) not in (None, "")) if field and field != "name" else 0
        if not field or field == "name" or have == 0:
            cand = {}
            for c in cs:
                for k in ("birth_year", "expiry_date", "since_date", "price"):
                    if policy._field(c, k) not in (None, ""):
                        cand[k] = cand.get(k, 0) + 1
            opts = [{"id": k, "label": f"Ask for {k.replace('_', ' ')} ({n} of {len(cs)} clients have it)"} for k, n in
                    sorted(cand.items(), key=lambda x: -x[1])[:3]]
            out.append(_gap("q_identity", "identity_check",
                            "Before sharing contract details the agent asks a question, but the answer cannot be checked against your file. "
                            "Which detail should the client confirm?",
                            "'Am I speaking with the insured?' verifies nothing: anyone can say yes.", "gdpr-art-5",
                            opts + [{"id": "none", "label": "Share no contract details on the phone"}],
                            {"value": idc, "effect": "the agent shares only the name and the reason for the call"},
                            len(cs), sum(_value(c) for c in cs)))
    if not s.get("country"):
        out.append(_gap("q_country", "country", "Which country do your clients live in?",
                        "The law pack (consent, hours, wording) depends on it; unknown means the strictest rules.", "eu-eprivacy-13",
                        [{"id": "GR", "label": "Greece"}, {"id": "other", "label": "Another country"}],
                        {"value": None, "effect": "the strictest known rules apply"}, len(cs), sum(_value(c) for c in cs)))
    if pk["lanes"].get("human_calls") in ("allowed", "allowed_unless_opt_out") and s.get("human_calls_allowed") is None:
        no_consent = [c for c in cs if policy.consent_ok(c, s)["how"] in ("none", "mark")]
        if no_consent:
            out.append(_gap("q_human_calls", "human_calls_allowed",
                            f"{len(no_consent)} clients cannot be called by the AI. May your staff phone them (you check the do-not-call register)?",
                            "Human marketing calls have their own rules; Prova only prepares the brief.", ref,
                            [{"id": "yes", "label": "Yes, my staff may call them"}, {"id": "no", "label": "No"}],
                            {"value": False, "effect": "they stay on consent request"},
                            len(no_consent), sum(_value(c) for c in no_consent)))
    return out


def attest_all(by: str = "owner") -> dict:
    """The owner answered 'all clients consented to automated calls': record it for rows with no value (never for a 'no').
    Only the owner's answer path may call this (the guard denies setup agents)."""
    n = 0
    for c in db.q("SELECT * FROM customer"):
        if (c.get("consent") or {}).get("automated_calls") is None and c.get("status") not in ("do_not_call",):
            db.update("customer", c["id"], consent={"automated_calls": True, "source": f"attested: {by}", "at": db.now_iso()})
            n += 1
    return {"updated": n}


# ---------------------------------------------------------------- consent requests (web links only, never SMS)

def consent_text(lang: str) -> str:
    company = _company()
    if lang == "el":
        return (f"{company} θα ήθελε να σας καλέσει με ψηφιακό βοηθό (τεχνητή νοημοσύνη) για το συμβόλαιό σας. "
                "Η κλήση καταγράφεται. Μπορείτε να αρνηθείτε ή να ανακαλέσετε τη συναίνεσή σας οποιαδήποτε στιγμή.")
    return (f"{company} would like to call you with a digital assistant (artificial intelligence) about your contract. "
            "The call is recorded. You can refuse or withdraw your consent at any time.")


def _hash(text: str) -> str:
    return hashlib.sha256(f"{CONSENT_VERSION}|{text}".encode()).hexdigest()[:16]


def send_consent_requests(customer_ids=None) -> dict:
    """One consent_request row (a web link, channel 'web') per customer on the consent_request lane. No SMS, no e-mail."""
    out, skipped = [], []
    for c in _customers():
        if customer_ids is not None and c["id"] not in customer_ids:
            continue
        r = lane(c)
        if r["lane"] != "consent_request":
            skipped.append({"customer_id": c["id"], "lane": r["lane"], "rule": r["rule"]})
            continue
        lang = policy.call_language(c)
        msg = consent_text(lang)
        row = db.one("SELECT * FROM consent_request WHERE customer_id=? AND status='sent' ORDER BY id DESC LIMIT 1", (c["id"],))
        if row and row["text_hash"] == _hash(msg):
            out.append({"customer_id": c["id"], "name": c["name"], "token": row["token"], "url": f"/k/{row['token']}",
                        "inbox_url": f"/inbox/{row['token']}", "reused": True})
            continue
        token = secrets.token_urlsafe(12)
        db.insert("consent_request", token=token, customer_id=c["id"], channel="web", message=msg, text_hash=_hash(msg),
                  status="sent", sent_at=db.now_iso())
        db.emit("consent", customer_id=c["id"], payload={"customer_id": c["id"], "kind": "requested", "channel": "web", "text_hash": _hash(msg)})
        out.append({"customer_id": c["id"], "name": c["name"], "token": token, "url": f"/k/{token}", "inbox_url": f"/inbox/{token}"})
    return {"sent": len(out), "links": out, "skipped": len(skipped), "channel": "web"}


def _request(token: str):
    if not token or len(str(token)) < 8:
        return None
    return db.one("SELECT * FROM consent_request WHERE token=?", (str(token),))


def _expired(row: dict) -> bool:
    if row.get("status") == "expired":
        return True
    try:
        return db.now() - db.now().fromisoformat(row["sent_at"]) > timedelta(days=TOKEN_DAYS)
    except (TypeError, ValueError):
        return False


def consent_page(token: str) -> dict:
    """Data for /k/{token}: {company, who, why, text, text_version, language, customer_name, token, status, inbox_url}."""
    row = _request(token)
    if not row:
        return {"error": "invalid link"}
    if _expired(row):
        return {"error": "this link is no longer valid", "status": "expired"}
    c = db.get("customer", row["customer_id"]) or {}
    lang = policy.call_language(c)
    return {"company": _company(), "who": _company(), "why": "contract renewal", "text": row.get("message") or consent_text(lang),
            "text_version": CONSENT_VERSION, "language": lang, "customer_name": c.get("name"), "token": row["token"],
            "status": row["status"], "inbox_url": f"/inbox/{row['token']}"}


def consent_answer(token: str, choice: str, when: str | None = None) -> dict:
    """The customer's tap. choice: call_now | scheduled | stop.
    call_now -> consent recorded (source 'tap'); returns what the REST layer needs to start the browser call:
      {ok, status, token, customer_id, start_call: True, ws: '/api/phone/ws?ct=<token>'}
    scheduled -> consent recorded + a followup row at `when`. stop -> do_not_call + consent withdrawn."""
    row = _request(token)
    if not row:
        return {"error": "invalid link"}
    if _expired(row):
        return {"error": "this link is no longer valid"}
    c = db.get("customer", row["customer_id"])
    if not c:
        return {"error": "invalid link"}
    now = db.now_iso()
    h = row.get("text_hash") or _hash(row.get("message") or "")
    if choice == "stop":
        db.update("customer", c["id"], status="do_not_call", consent={"automated_calls": False, "source": "tap", "at": now,
                                                                         "request_id": row["id"]})
        db.q("UPDATE followup SET status='cancelled' WHERE customer_id=? AND status='pending'", (c["id"],))
        db.update("consent_request", row["id"], status="stopped", answered_at=now)
        db.emit("consent", customer_id=c["id"], payload={"customer_id": c["id"], "kind": "withdrawn", "channel": row.get("channel") or "web", "text_hash": h})
        return {"ok": True, "status": "stopped", "token": row["token"], "customer_id": c["id"]}
    if choice not in ("call_now", "scheduled"):
        return {"error": f"unknown choice {choice!r}"}
    if c.get("status") == "do_not_call":
        return {"error": "this customer asked not to be called; ask the office"}
    due = None
    if choice == "scheduled":
        due = _parse_when(when)
        if due is None:
            return {"error": "pick a date and time"}
    db.update("customer", c["id"], consent={"automated_calls": True, "source": "tap", "at": now, "request_id": row["id"]})
    db.emit("consent", customer_id=c["id"], payload={"customer_id": c["id"], "kind": "given", "channel": row.get("channel") or "web", "text_hash": h})
    if choice == "call_now":
        db.update("consent_request", row["id"], status="call_now", answered_at=now)
        return {"ok": True, "status": "call_now", "token": row["token"], "customer_id": c["id"], "start_call": True,
                "ws": f"/api/phone/ws?ct={row['token']}", "inbox_url": f"/inbox/{row['token']}"}
    db.update("consent_request", row["id"], status="scheduled", answered_at=now, scheduled_for=due.isoformat(timespec="minutes"))
    db.q("UPDATE followup SET status='cancelled' WHERE customer_id=? AND status='pending'", (c["id"],))
    fid = db.insert("followup", customer_id=c["id"], source_call_id=None, due_at=due.isoformat(timespec="seconds"), reason="callback", status="pending")
    db.emit("followup", customer_id=c["id"], payload={"due_at": due.isoformat(timespec="seconds"), "reason": "callback"})
    return {"ok": True, "status": "scheduled", "token": row["token"], "customer_id": c["id"], "followup_id": fid,
            "scheduled_for": due.isoformat(timespec="minutes"), "inbox_url": f"/inbox/{row['token']}"}


def _parse_when(when):
    if not when:
        return None
    try:
        from datetime import datetime
        d = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone().replace(tzinfo=None)
    except ValueError:
        return policy.parse_when(when)


def join(name: str, phone: str, language: str, consent_text_version: str) -> dict:
    """/join (QR): create a demo customer (a policy expiring in 3 days) from the form, then record the tap (call_now)."""
    name, phone = str(name or "").strip(), re.sub(r"[^\d+]", "", str(phone or ""))
    if not name or len(re.sub(r"\D", "", phone)) < 6:
        return {"error": "name and phone are required"}
    if consent_text_version and consent_text_version != CONSENT_VERSION:
        return {"error": "the consent text changed; reload the page"}
    lang = str(language or "el").lower()[:2]
    s = _s()
    offer = next((o for o in s.get("offers") or [] if isinstance(o, dict)), {})
    expiry = (db.now() + timedelta(days=3)).date().isoformat()
    existing = db.one("SELECT * FROM customer WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,))
    fields = {"name": name, "phone": phone, "language": lang, "product": offer.get("name") or offer.get("id") or "Demo policy",
              "price": policy.to_float(offer.get("price")), "period": offer.get("period") or "year", "expiry_date": expiry,
              "since_date": (db.now() - timedelta(days=3 * 365)).date().isoformat(), "extra": {"joined_via": "qr"}}
    if existing:
        cid = existing["id"]
        db.update("customer", cid, fields={**(existing.get("fields") or {}), **fields}, name=name)
    else:
        cid = db.insert("customer", row_no=0, raw={"joined": True}, fields=fields, name=name, phone=phone,
                        consent={"automated_calls": None, "source": None, "at": None}, status="active", flags=["joined via qr"], notes="")
    msg = consent_text(lang)
    token = secrets.token_urlsafe(12)
    db.insert("consent_request", token=token, customer_id=cid, channel="qr", message=msg, text_hash=_hash(msg), status="sent", sent_at=db.now_iso())
    res = consent_answer(token, "call_now")
    return {**res, "customer_id": cid} if "error" not in res else res


# ---------------------------------------------------------------- human lane

def queue_human(customer_ids=None) -> dict:
    """Handoff rows (reason 'human_lane') with a context card, for customers on the human_call lane."""
    pk = _pack()
    queued = []
    for c in _customers():
        if customer_ids is not None and c["id"] not in customer_ids:
            continue
        if lane(c)["lane"] != "human_call":
            continue
        if db.one("SELECT id FROM handoff WHERE customer_id=? AND reason='human_lane' AND status='waiting'", (c["id"],)):
            continue
        f = c.get("fields") or {}
        ctx = {"customer": c["name"], "phone": c["phone"], "product": f.get("product"), "expiry_date": f.get("expiry_date"),
               "offers": [{"id": o.get("id"), "name": o.get("name"), "price": policy.list_price(o)} for o in policy.eligible_offers(c)],
               "max_discount_pct": policy.discount_limit(c), "contact": _s().get("handoff_contact"), "language": policy.call_language(c)}
        hid = db.insert("handoff", customer_id=c["id"], call_id=None, reason="human_lane", context=ctx, status="waiting", created_at=db.now_iso())
        queued.append(hid)
    out = {"queued": len(queued), "handoff_ids": queued}
    if pk["lanes"].get("human_calls") not in ("allowed", "allowed_unless_opt_out"):
        out["note"] = f"human calls are {pk['lanes'].get('human_calls')} for {pk['country']}: nobody is queued"
    return out


# ---------------------------------------------------------------- the Compliance Brief

def _industry_applies(item: dict, s: dict) -> bool:
    need = (item.get("applies") or {}).get("industry")
    if not need:
        return True
    return bool(re.search(r"insur|ασφαλ", policy.norm(f"{s.get('business_type') or ''} {s.get('company_name') or ''}")))


def brief() -> dict:
    """What law applies (verified or not), what Prova enforces, what stays with the owner, lanes, attestations."""
    s = _s()
    pk = _pack(s)
    laws = [{"id": i["id"], "title": i["title"], "source_id": i["source_id"], "quote": i["quote"], "verified": i["verified"],
             "label": i["label"], "requires": i["requires"], "prova_enforces": i["prova_enforces"], "owner_must": i["owner_must"],
             "url": i["url"]} for i in pk["laws"] if _industry_applies(i, s)]
    stored = {r["key"]: r for r in db.q("SELECT * FROM policy_slot WHERE status<>'empty'")}
    keys = ("country", "consent_basis", "call_hours", "must_say", "blocked_segments", "human_calls_allowed", "max_discount_pct",
            "identity_check", "max_attempts", "min_gap_hours")
    slots = {k: {"value": s.get(k), "source": (stored.get(k) or {}).get("source") or "default", "status": (stored.get(k) or {}).get("status") or "default"}
             for k in keys}
    basis = s.get("consent_basis") if isinstance(s.get("consent_basis"), dict) else {}
    attest = [{"key": k, "source": r["source"], "status": r["status"]} for k, r in stored.items()
              if r["status"] == "confirmed" and "attested" in str(r["source"])]
    unchecked = []
    if basis.get("automated_calls") in ("all", "some") and not basis.get("clause_checked"):
        unchecked.append("consent_basis: the owner says clients consented, but no consent clause was checked against automated calls")
    lanes = lanes_summary()
    last = db.one("SELECT * FROM eval_run ORDER BY id DESC LIMIT 1")
    unverified = [i["id"] for i in laws if not i["verified"]]
    return {"country": pk["country"], "laws": laws, "unverified_law_items": unverified, "slots": slots, "lanes": lanes,
            "lane_rules": {"ai_call": "automated-call consent on record", "consent_request": f"web link; law item {_REQUEST_LAW_ITEM} (verified)",
                           "human_call": f"human calls: {pk['lanes'].get('human_calls')}", "hold": "no lawful channel"},
            "attestations": attest, "unchecked_attestations": unchecked,
            "owner_todos": ["Check the national do-not-call register before any human call.", "Keep a record of where each consent was given.",
                            "Decide how long call recordings are kept.", "Confirm unverified law items with counsel."],
            "certification": {"passed": last["passed"], "failed": last["failed"], "status": last["status"]} if last else None,
            "floor_version": CONSENT_VERSION, "disclaimer": "Not legal advice. Items marked unverified never allow anything; confirm with counsel."}
