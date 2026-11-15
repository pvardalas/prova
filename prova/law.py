"""Law data. Data only. Every item carries a `source_id` from docs/law_sources.md and a `quote` copied character
for character from that file (tests/test_law.py checks it). Items whose source status there is S-only or X have
`verified: False`: the Compliance Brief shows them as "unverified, confirm with counsel" and they never make a
lane or an action allowed. Nothing here is written from memory. Not legal advice.
"""
from copy import deepcopy

SOURCES_FILE = "docs/law_sources.md"


def _item(id, title, source_id, quote, verified, requires, prova_enforces, owner_must="", applies=None, url=""):
    return {"id": id, "title": title, "source_id": source_id, "quote": quote, "verified": verified,
            "applies": applies or {}, "requires": requires, "prova_enforces": prova_enforces,
            "owner_must": owner_must, "ref": source_id, "url": url}


EU_LAWS = [
    _item("eu-eprivacy-13", "Direct marketing by automated calling needs prior consent (ePrivacy art. 13)", "eu-eprivacy-13",
          "may be allowed only in respect of subscribers or users who have given their prior consent", True,
          "Automated calling systems for direct marketing only with the subscriber's prior consent.",
          "A customer without recorded consent is never dialled by the AI (lane ai_call needs consent).",
          url="https://www.legislation.gov.uk/eudr/2002/58/article/13", applies={"channel": "automated_call"}),
    _item("eu-eprivacy-13-2", "Own customers: similar products (ePrivacy art. 13(2))", "eu-eprivacy-13",
          "may use these electronic contact details for direct marketing of its own similar products or services", True,
          "Contact details obtained in a sale may be used for similar own products, with free easy objection at collection and in each message.",
          "Only consent requests (web link / e-mail) go to existing customers; never an automated call.",
          "Offer the objection option in every message.", url="https://www.legislation.gov.uk/eudr/2002/58/article/13",
          applies={"channel": "consent_request"}),
    _item("eu-ai-act-50", "Tell people they are talking to an AI (AI Act art. 50)", "eu-ai-act-50",
          "People must be notified when they are interacting with an AI system from the start of the first interaction", True,
          "Disclosure of the AI from the start of the first interaction.",
          "The fixed opening line says it is an AI assistant (floor ai_disclosure).",
          url="https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act"),
    _item("eu-idd-20", "Demands and needs before an insurance contract (IDD art. 20)", "eu-idd-20",
          "shall specify, on the basis of information obtained from the customer, the demands and the needs of that customer", True,
          "Before the contract the distributor specifies the customer's demands and needs.",
          "Not enforced in the MVP; shown in the brief for insurance.", "Check your own process records demands and needs.",
          url="https://www.legislation.gov.uk/eudr/2016/97/article/20", applies={"industry": "insurance"}),
    _item("eu-idd-23", "Telephone sale: information in writing right after (IDD art. 23)", "eu-idd-23",
          "given under the EU distance-marketing rules for financial services", True,
          "Pre-contract information incl. the product information document, on paper or durable medium immediately after a telephone sale.",
          "The confirmation page carries the product information document as a downloadable durable medium.",
          url="https://www.legislation.gov.uk/eudr/2016/97/article/23", applies={"industry": "insurance"}),
    _item("eu-eprivacy-2h", "Electronic mail definition (ePrivacy art. 2(h))", "eu-eprivacy-2h",
          "any text, voice, sound or image message sent over a public communications network", True,
          "SMS is not named; its inclusion is an inference.", "No SMS is sent by Prova (web-only channels).",
          url="https://www.legislation.gov.uk/eudr/2002/58/article/2"),
    _item("eu-gdpr-21", "Right to object to direct marketing (GDPR art. 21)", "eu-gdpr-21",
          "the data subject shall have the right to object at any time", False,
          "The right to object is brought explicitly to the person's attention at the latest at the first communication.",
          "Floor must_say at the opening: the right to object at any time and how.", url="https://gdpr-info.eu/art-21-gdpr/"),
    _item("eu-gdpr-13", "Information at collection (GDPR art. 13)", "eu-gdpr-13",
          "purposes and legal basis", False, "Controller identity and contact, purposes and legal basis at collection.",
          "The consent page states who asks and why.", url="https://gdpr-info.eu/art-13-gdpr/"),
    _item("eu-gdpr-22", "No decision based solely on automated processing (GDPR art. 22)", "eu-gdpr-22",
          "not to be subject to a decision based solely on automated processing", False,
          "Right not to be subject to solely automated decisions with legal or similar effect.",
          "Nothing is final on the phone; the customer confirms in writing (floor).", url="https://gdpr-info.eu/art-22-gdpr/"),
]

GR_LAWS = [
    _item("gr-3471-11", "Automated calls and unsolicited communication need prior explicit consent (ν. 3471/2006 art. 11)",
          "gr-3471-11", "επιτρέπεται μόνο αν ο συνδρομητής συγκατατεθεί εκ των προτέρων ρητώς", False,
          "Automated calling and any unsolicited electronic communication for direct marketing, with or without human intervention, only with prior explicit consent (original 2006 text, not consolidated).",
          "Human-call lane stays off until the consolidated text is stored.", "Confirm the current text with counsel.",
          url="https://www.dsanet.gr/Epikairothta/Nomothesia/n3471_06.htm"),
    _item("gr-4583-33-7", "Telephone sale of insurance: information in writing (ν. 4583/2018 art. 33 §7)", "gr-4583-33-7",
          "Στην περίπτωση πώλησης μέσω τηλεφώνου", False,
          "Pre-contract information incl. the product information document; in writing immediately after conclusion.",
          "Confirmation page carries the product information document.", url="https://www.taxheaven.gr/law/4583/2018/arthro/33",
          applies={"industry": "insurance"}),
]

# Wording Prova adds to the opening because of eu-gdpr-21 (design change 2 in docs/law_sources.md). Our text, not law text.
FLOOR_MUST_SAY = [{"stage": "opening", "law_item": "eu-gdpr-21", "floor": True,
                   "text": {"el": "Μπορείτε να αντιταχθείτε οποτεδήποτε στις κλήσεις αυτές, απλώς πείτε το μου και θα σταματήσουν.",
                            "en": "You can object to calls like this at any time: just tell me and they stop."}}]

JURISDICTIONS = {
    "EU": {"laws": EU_LAWS,
           "lanes": {"consent_request_channels": ["web", "email"], "human_calls": "disabled_until_source"},
           "call_hours": {"days": [1, 2, 3, 4, 5], "from": "09:00", "to": "20:00", "source": "prova_default: no source in docs/law_sources.md"},
           "floor_must_say": FLOOR_MUST_SAY},
    "GR": {"extends": "EU", "laws": GR_LAWS,
           "lanes": {"human_calls": "disabled_until_source"},
           "opening": {"el": "Καλημέρα, είμαι ψηφιακός βοηθός (τεχνητή νοημοσύνη) της εταιρείας.",
                       "en": "Hello, I am an AI assistant calling for the company."}},
}


def pack(country: str | None) -> dict:
    """EU base merged with the country overlay. Unknown or missing country -> the strictest known (GR overlay)."""
    c = (country or "").upper()
    if c not in JURISDICTIONS or c == "EU":
        c = "GR" if c not in JURISDICTIONS else "EU"
    out = deepcopy(JURISDICTIONS["EU"])
    node = JURISDICTIONS[c]
    if c != "EU":
        out["laws"] = out["laws"] + deepcopy(node.get("laws", []))
        out["lanes"] = {**out["lanes"], **node.get("lanes", {})}
        for k in ("opening", "call_hours", "floor_must_say"):
            if k in node:
                out[k] = deepcopy(node[k])
    out["country"] = c
    out["laws"] = [dict(i, label="verified" if i["verified"] else "unverified, confirm with counsel") for i in out["laws"]]
    return out


def verified_ids(country: str | None) -> set:
    return {i["id"] for i in pack(country)["laws"] if i["verified"]}
