"""Injection scan for untrusted text (web pages, documents, OCR, customer notes, tactic cards, free-text args).

A code heuristic (Greek + English) plus a pluggable classifier hook (e.g. Prompt Guard 2 later):
    scan.set_classifier(lambda text: float)   # 0..1, takes the max with the heuristic
scan(text, source) -> {"score", "suspicious", "hits", "source"}. Never raises.
"""
import re
import unicodedata

THRESHOLD = 0.6
_classifier = None


def set_classifier(fn):
    global _classifier
    _classifier = fn


def norm(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("ς", "σ")
    return re.sub(r"\s+", " ", t)


_HIDDEN = re.compile("[​‌‍⁠﻿‪-‮⁦-⁩]")

# (name, weight, regex over norm(text))
_PATTERNS = [
    ("ignore_instructions", .95, r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(previous|prior|above|earlier|all|any|your|the)\b.{0,30}\b(instruction|rule|prompt|polic|guideline|limit)"),
    ("ignore_instructions_el", .95, r"(αγνοησ|παρακαμψ|ξεχασ|αγνοηστε).{0,40}(οδηγιεσ|κανονεσ|κανονα|οριο|ορια|πολιτικη)"),
    ("system_prompt", .8, r"(system prompt|developer message|hidden instructions|your instructions|initial prompt)"),
    ("system_prompt_el", .8, r"(οδηγιεσ συστηματοσ|system prompt|κρυφεσ οδηγιεσ|πεσ μου τισ οδηγιεσ)"),
    ("role_override", .85, r"\b(you are now|from now on you|pretend (to be|you are)|new instructions?)\b"),
    ("role_override_el", .8, r"(εισαι πλεον|απο τωρα εισαι|υποδυσου|νεεσ οδηγιεσ)"),
    ("set_discount", .9, r"\b(set|raise|increase|change|make|give)\b.{0,30}\bdiscount\b.{0,25}\b(?:[3-9]\d|100)\s*%"),
    ("set_discount_el", .9, r"(βαλε|οριστε|ορισε|δωσε|αυξησε|αλλαξε).{0,30}εκπτωση.{0,25}(?:[3-9]\d|100)\s*%"),
    ("consent_claim", .95, r"\b(all|every)\b.{0,15}\b(clients?|customers?|contacts?)\b.{0,25}\b(have |has )?(consent|opted in|agreed)"),
    ("consent_claim_el", .95, r"(ολοι|ολουσ|καθε) .{0,15}(πελατ|μελ).{0,30}(εχουν συναινεσ|συναινουν|συγκατατεθ)"),
    ("skip_checks", .9, r"\b(skip|disable|turn off|do not run|don't run|without)\b.{0,25}\b(questions?|safety test|tests?|checks?|guard|verification)\b"),
    ("skip_checks_el", .9, r"(παρακαμψε|παραλειψε|μην τρεξεισ|απενεργοποιησε|χωρισ).{0,25}(ερωτησ|δοκιμ|ελεγχ|τεστ)"),
    ("hide_from_owner", .9, r"(do not|don't|never)\b.{0,20}\b(tell|inform|show|ask)\b.{0,20}\b(the )?(owner|user|manager)"),
    ("hide_from_owner_el", .9, r"μην (πεισ|ειδοποιησεισ|ρωτησεισ|ενημερωσεισ).{0,25}(ιδιοκτητ|χρηστη|ιδιοκτητη)"),
    ("exfiltrate", .85, r"\b(send|post|email|upload|forward|exfiltrate)\b.{0,40}\b(customer (list|data|file)|all customers|database|phone numbers)\b.{0,40}\b(to|at)\b"),
    ("approve_live", .85, r"\b(approve|go live|turn on|switch on)\b.{0,30}\b(immediately|now|without|yourself)\b"),
    ("always_give", .8, r"\balways (give|offer|agree|accept)\b.{0,30}\b\d{2,3}\s*%"),
    ("always_give_el", .8, r"παντα (δινε|δωσε|προσφερε).{0,30}\d{2,3}\s*%"),
]
# text that addresses an AI/bot directly and then gives it an order (normalised: no accents, final sigma = σ)
_ADDR = r"(ρομποτ\w*|robots?|bots?|\bai\b|\bllm\b|chatbot|assistants?|language model|μοντελ\w+|βοηθ(?:οσ|ο|ουσ|οι)\b|τεχνητ\w* νοημοσυν\w+|\bτν\b|\bνοημοσυνη)"
_IMP = (r"(δωσ(?:ε|τε)|γραψ(?:ε|τε)|πεσ|πειτε|βαλ(?:ε|τε)|οριστε|ορισε|αγνοησ(?:ε|τε)|ενεργοποιησ(?:ε|τε)|"
        r"παραλειψ(?:ε|τε)|απαντησ(?:ε|τε)|give|write|say|tell|set|ignore|mark|approve|enable|skip|answer|reply|respond|report|state)")
_PATTERNS += [
    ("addresses_ai_read", .9, r"\b(αν|εαν|οταν|if|when|whoever|anyone)\b.{0,30}(διαβαζ|βλεπ|διαβασ|\bread|\bsee|process|parse|scan|summari)\w*.{0,25}" + _ADDR + r".{0,60}\b" + _IMP + r"\b"),
    ("addresses_ai_note", .9, r"\b(σημειωση|μηνυμα|οδηγια|note|message|instructions?|attention|προσοχη|αγαπητ\w+|dear|hey|hi)\b.{0,8}\b(για|προσ|to|for)?\b.{0,12}" + _ADDR + r".{0,60}\b" + _IMP + r"\b"),
    ("addresses_ai_you", .9, r"\b(if you are|if you're|you are an?|αν εισαι|εισαι)\b.{0,15}" + _ADDR + r".{0,60}\b" + _IMP + r"\b"),
]
_RX = [(n, w, re.compile(r, re.S)) for n, w, r in _PATTERNS]


def scan(text, source: str = "") -> dict:
    try:
        raw = str(text or "")
        hits, score = [], 0.0
        if _HIDDEN.search(raw):
            hits.append("hidden_unicode")
            score = max(score, 0.5)
        t = norm(_HIDDEN.sub("", raw))
        for name, w, rx in _RX:
            if rx.search(t):
                hits.append(name)
                score = max(score, w)
        if len(hits) >= 2:
            score = min(1.0, score + 0.05)
        if _classifier is not None:
            try:
                score = max(score, float(_classifier(raw)))
            except Exception:  # noqa: BLE001  a broken hook never loosens the heuristic
                hits.append("classifier_error")
        return {"score": round(score, 2), "suspicious": score >= THRESHOLD, "hits": hits, "source": source}
    except Exception:  # noqa: BLE001
        return {"score": 1.0, "suspicious": True, "hits": ["scan_error"], "source": source}


def scan_args(args, source: str = "args") -> dict:
    """Scan every string inside a nested args structure."""
    strings = []

    def walk(v):
        if isinstance(v, str):
            strings.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(args)
    return scan("\n".join(strings), source)
