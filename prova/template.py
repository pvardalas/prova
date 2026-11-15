"""Fixed templates. Data only, no logic. The agent FILLS these; it never invents new hard rules.

POLICY_SLOTS: every company policy is one of these slots. Each slot has exactly one enforcement point
(see SPEC.md section 4) and yields exactly one eval check. Anything a company wants that does not fit a
slot goes into `soft_rules` (prompt + sidecar only, shown in the UI as "not hard-enforced").

FLOOR: legal minimums a company cannot switch off. policy.py applies them on top of the slots.
GOALS: goal templates. Each defines what "success" means so a call's result can be checked.
"""

# key, type, required, group, label, default (None = must be filled from documents or by the owner)
POLICY_SLOTS = [
    # --- who we are
    {"key": "company_name", "type": "text", "required": True, "group": "identity", "label": "Company name as spoken on calls", "default": None},
    {"key": "business_type", "type": "text", "required": True, "group": "identity", "label": "Business type (insurance broker, gym, ...)", "default": None},
    {"key": "language", "type": "text", "required": True, "group": "identity", "label": "Call language (ISO code)", "default": "el"},
    {"key": "handoff_contact", "type": "text", "required": True, "group": "identity", "label": "Human who takes over (name and phone)", "default": None},
    # --- what we may sell
    {"key": "offers", "type": "list", "required": True, "group": "offers",
     "label": "Offers the agent may sell: [{id, name, price, period, details, eligibility}]", "default": None},
    {"key": "max_discount_pct", "type": "number", "required": True, "group": "offers", "label": "Highest discount the agent may give (%)", "default": None},
    {"key": "discount_rules", "type": "list", "required": False, "group": "offers",
     "label": "Narrower discount limits: [{when: text, field: customer field, op: '>=' | '<=' | '==', value, max_discount_pct}]", "default": []},
    {"key": "forbidden_claims", "type": "list", "required": False, "group": "offers",
     "label": "Things the agent must never say or promise", "default": []},
    # --- how we may call
    {"key": "allowed_purposes", "type": "list", "required": True, "group": "calling",
     "label": "Call purposes the company runs (renew, remind, book, win_back, collect)", "default": None},
    {"key": "max_attempts", "type": "number", "required": True, "group": "calling", "label": "Most calls to one customer per campaign", "default": 3},
    {"key": "min_gap_hours", "type": "number", "required": True, "group": "calling", "label": "Least hours between two calls to one customer", "default": 24},
    # --- the call itself
    {"key": "identity_check", "type": "object", "required": True, "group": "call",
     "label": "Before account details are shared, the customer must confirm this: {field: customer field, question: text}", "default": None},
    {"key": "recording", "type": "object", "required": True, "group": "call",
     "label": "Call recording: {on: bool, notice: text spoken at the start}", "default": {"on": True, "notice": None}},
    {"key": "escalation_triggers", "type": "list", "required": False, "group": "call",
     "label": "Situations that must go to a human (complaint, legal threat, ...)", "default": ["complaint", "legal threat", "bereavement"]},
    # --- closing
    {"key": "confirmation", "type": "object", "required": True, "group": "closing",
     "label": "How an agreement becomes binding: {channel: 'sms' | 'email', withdrawal_text: text}", "default": None},
    # --- v3 slots (country, consent, hours, wording)
    {"key": "country", "type": "text", "required": True, "group": "identity",
     "label": "Country (ISO code), inferred from the site address and phone prefixes", "default": None},
    {"key": "consent_basis", "type": "object", "required": False, "group": "calling",
     "label": "Consent basis: {automated_calls: all|some|none|unknown, covers_automated: bool|null, marks_mean_yes: bool|null, where: text, clause_checked: bool}",
     "default": {"automated_calls": "unknown"}},
    {"key": "call_hours", "type": "object", "required": False, "group": "calling",
     "label": "Calling window: {days: [1..7], from: 'HH:MM', to: 'HH:MM'} (the law pack fills it when empty)", "default": None},
    {"key": "must_say", "type": "list", "required": False, "group": "call",
     "label": "Sentences the agent must say: [{stage: opening|before_agreement|closing, text}]", "default": []},
    {"key": "blocked_segments", "type": "list", "required": False, "group": "calling",
     "label": "Conditions (DSL) that put matching customers on hold", "default": []},
    {"key": "opening_lines", "type": "object", "required": False, "group": "call",
     "label": "Opening line per language: {lang: text}, generated and back-checked", "default": {}},
    {"key": "human_calls_allowed", "type": "bool", "required": False, "group": "calling",
     "label": "Staff may phone clients who gave no consent for automated calls (owner checks the opt-out register)", "default": None},
    # --- soft
    {"key": "soft_rules", "type": "list", "required": False, "group": "soft",
     "label": "Other guidance (tone, wording). NOT hard-enforced.", "default": []},
]

SLOT_KEYS = [s["key"] for s in POLICY_SLOTS]
REQUIRED_KEYS = [s["key"] for s in POLICY_SLOTS if s["required"]]

# Legal floor: applies whatever the slots say. Designed for Greece / EU; not legal advice.
FLOOR = {
    "ai_disclosure": True,                 # first agent turn says it is an AI assistant calling for the company
    "consent_required": True,              # automated call needs a recorded consent for that purpose
    "honour_opt_out": True,                # "stop calling" ends the call and blocks future calls at once
    "max_attempts_cap": 999,               # effectively no floor: the company's own max_attempts applies
    "min_gap_hours_floor": 0,               # no floor: the company's own min_gap_hours applies (0 = rule off)
    "sale_needs_written_confirmation": True,  # nothing is final on the phone; a confirmation link is sent
    "no_voice_emotion_inference": True,    # the sidecar reads words only
    "vulnerable_customer_stop": True,      # confused / distressed / very elderly customer: stop selling, offer a human
}

# Goal templates. success_tool = the gateway tool that marks the goal as reached on a call.
GOALS = {
    "renew": {"label": "Renew contracts or policies that are about to end", "success_tool": "record_agreement",
              "needs": ["expiry_date"], "metric": "renewed value per month"},
    "win_back": {"label": "Win back customers who left or lapsed", "success_tool": "record_agreement",
                 "needs": ["last_active_date"], "metric": "customers won back"},
    "remind": {"label": "Remind customers of something due (appointment, payment, inspection)", "success_tool": "record_acknowledgement",
               "needs": ["due_date"], "metric": "customers who confirmed"},
    "book": {"label": "Book an appointment", "success_tool": "record_booking", "needs": [], "metric": "appointments booked"},
    "collect": {"label": "Agree a payment date for an overdue amount", "success_tool": "record_promise",
                "needs": ["amount_due"], "metric": "amount promised"},
}

# Canonical customer fields the ingestor maps raw columns onto. Unmapped columns are kept in `extra`.
CUSTOMER_FIELDS = {
    "name": "full name", "phone": "phone number (stored E.164, +30...)", "email": "email",
    "product": "what the customer has with the company (policy, plan, membership)",
    "price": "what they pay now (number, EUR)", "period": "what the price covers (month, 6 months, year)",
    "expiry_date": "when the contract / policy / membership ends (ISO date)",
    "last_active_date": "last visit, purchase or payment (ISO date)",
    "due_date": "date of the thing to be reminded (ISO date)", "amount_due": "overdue amount (number, EUR)",
    "since_date": "customer since (ISO date)", "birth_year": "year of birth (number)",
    "consent": "did the customer agree to automated calls (true / false / unknown)",
    "do_not_call": "customer asked not to be called (true / false)",
    "notes": "free-text notes the company keeps on the customer",
    "language": "call language, ISO code",
    "opt_out_register": "number is on the national do-not-call register, true/false",
}
