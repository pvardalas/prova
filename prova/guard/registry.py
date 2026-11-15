"""Every tool of every agent, declared once. guard.check refuses anything not declared here.

Entry: {name, roles, phases, effect, schema, rate, needs_human, scope}
  effect: read | propose | write_internal | external | irreversible
  scope (for roles mcp / owner on REST): read < setup < runtime < owner ("owner" = a human, never an external agent)
  schema: {"props": {key: {type, enum?, max?}}, "required": [...], "lenient": bool}
          types: string number integer boolean array object any. Unknown keys are always rejected.
"""
from functools import lru_cache

EFFECTS = ("read", "propose", "write_internal", "external", "irreversible")
SCOPES = ("read", "setup", "runtime", "owner")
CALL_PHASES = ["warmup", "sim", "eval", "live"]
SETUP_PHASES = ["READ", "INFER", "ASK", "COMPILE", "CERTIFY", "AWAIT_TURN_ON"]
ANY_PHASE: list = []

# Words an agent might use for something it must never be able to do: denied with a specific rule and hint.
FORBIDDEN = {
    "approve": "human_tap_only", "approve_and_go_live": "human_tap_only", "go_live": "human_tap_only", "turn_on": "human_tap_only",
    "set_consent": "consent_needs_owner", "mark_consented": "consent_needs_owner", "set_customer_consent": "consent_needs_owner",
    "answer_own_question": "no_self_answer", "contact_customer": "no_contact_during_setup", "send_message": "no_contact_during_setup",
    "delete_customer": "no_bulk_delete", "bulk_update_customers": "no_bulk_delete", "edit_floor": "floor_is_read_only",
    "edit_law_pack": "floor_is_read_only", "set_budget": "budget_is_owner_only", "http_get": "no_generic_http",
    "fetch_url": "no_generic_http", "shell": "no_shell", "run_sql": "no_sql", "exec": "no_shell", "skip_safety_test": "human_tap_only",
}
FORBIDDEN_HINT = "That action is not available to an agent. Ask the owner (propose a question) or use one of your listed tools."


def S(*specs, lenient=False, required=None) -> dict:
    """S("customer_id:integer", "mode:string?", "note:string:500?") -> schema. '?' = optional."""
    props, req = {}, []
    for sp in specs:
        opt = sp.endswith("?")
        parts = sp.rstrip("?").split(":")
        d = {"type": parts[1] if len(parts) > 1 else "string"}
        if len(parts) > 2:
            d["max"] = int(parts[2])
        props[parts[0]] = d
        if not opt:
            req.append(parts[0])
    return {"props": props, "required": req if required is None else required, "lenient": lenient}


def _t(name, roles, effect, schema, phases=None, rate="", needs_human=False, scope="runtime"):
    return {"name": name, "roles": roles, "phases": phases if phases is not None else ANY_PHASE, "effect": effect,
            "schema": schema, "rate": rate, "needs_human": needs_human, "scope": scope}


_GATE_EFFECT = {"get_offers": "read", "verify_identity": "read", "get_account_details": "read", "quote_price": "read",
                "best_move": "read"}


def _gateway_entries() -> list:
    from prova import gateway
    out = []
    for d in gateway.TOOLS:
        props = {}
        for k, v in (d.get("parameters") or {}).get("properties", {}).items():
            props[k] = {"type": "number" if v["type"] == "NUMBER" else "string", "max": 2000}
        out.append(_t(d["name"], ["caller_agent"], _GATE_EFFECT.get(d["name"], "write_internal"),
                      {"props": props, "required": [], "lenient": True}, phases=CALL_PHASES, rate="300/session"))
    return out


# ---- setup tools (IMPLEMENTATION 7.1). Phase = the setup phase in which the tool may be called.
_SETUP = [
    _t("crawl_site", ["setup_agent", "mcp"], "read", S("url:string:500"), ["READ"], "30/min, 30/session", scope="setup"),
    _t("read_file", ["setup_agent", "mcp"], "read", S("path:string:500"), ["READ"], "20/session", scope="setup"),
    _t("read_image", ["setup_agent", "mcp"], "read", S("path:string:500"), ["READ"], "50/session", scope="setup"),
    _t("map_columns", ["setup_agent", "mcp"], "propose", S("path:string:500?"), ["READ", "INFER"], "20/session", scope="setup"),
    _t("propose_fact", ["setup_agent", "mcp"], "propose",
       S("key:string:60", "value:any", "source_quote:string:400", "source_ref:string:300"), ["INFER", "ASK", "COMPILE"],
       "30/min, 300/session", scope="setup"),
    _t("propose_goal", ["setup_agent", "mcp"], "propose", S("goal_type:string:40", "reason:string:400?"), ["INFER"], "10/session", scope="setup"),
    _t("propose_pack", ["setup_agent", "mcp"], "propose", S("pack:object"), ["INFER"], "5/session", scope="setup"),
    _t("propose_question", ["setup_agent", "mcp"], "propose",
       S("fact_key:string:60", "text:string:600", "why:string:600?", "options:array?"), ["ASK"], "10/session", scope="setup"),
    _t("wait_answer", ["setup_agent"], "read", S("qid:string:60"), ["ASK"], "60/session", scope="setup"),
    _t("compile", ["setup_agent", "mcp"], "write_internal", S(), ["COMPILE"], "10/session", scope="setup"),
    _t("propose_rule", ["setup_agent", "mcp"], "propose", S("rule:object"), ["COMPILE"], "20/session", scope="setup"),
    _t("run_certification", ["setup_agent", "mcp"], "write_internal", S(), ["CERTIFY"], "5/session", scope="setup"),
    _t("request_turn_on", ["setup_agent", "mcp"], "write_internal", S(), ["AWAIT_TURN_ON"], "3/session", scope="setup"),
    _t("turn_on", ["owner"], "irreversible", S("token:string:200"), needs_human=True, scope="owner"),
]

# ---- MCP tools. Scope says how much a bearer / MCP actor needs. No MCP tool is owner scope except via confirmation.
_MCP = [
    ("prova_guide", "read", "read", S()), ("prova_status", "read", "read", S()), ("list_samples", "read", "read", S()),
    ("get_policy", "read", "read", S()), ("get_playbook", "read", "read", S("goal:string:40?")),
    ("propose_goals", "read", "read", S()), ("playbook_history", "read", "read", S("goal:string:40?")), ("lab_overview", "read", "read", S()),
    ("list_customers", "read", "read", S("status:string:40?", "limit:integer?")), ("get_customer", "read", "read", S("customer_id:integer")),
    ("list_rejects", "read", "read", S()), ("list_calls", "read", "read", S("customer_id:integer?", "mode:string:20?", "limit:integer?")),
    ("get_call", "read", "read", S("call_id:integer", "max_turns:integer?")), ("report", "read", "read", S()),
    ("list_followups", "read", "read", S()), ("list_handoffs", "read", "read", S()), ("get_evidence", "read", "read", S("call_id:integer")),
    ("recent_events", "read", "read", S("after_id:integer?", "limit:integer?")), ("wait_for_job", "read", "read", S("job:string:60", "timeout_s:integer?")),
    ("export_customer", "read", "read", S("customer_id:integer")),
    ("setup_status", "read", "read", S()), ("compliance_brief", "read", "read", S()), ("get_brief", "read", "read", S()),
    ("find_due", "read", "read", S("days:integer?")),
    ("add_data_file", "setup", "write_internal", S("path:string:500")), ("add_document_file", "setup", "write_internal", S("path:string:500")),
    ("add_document_url", "setup", "write_internal", S("url:string:500")),
    ("read_documents", "setup", "write_internal", S("goal:string:600")), ("confirm_policy_slot", "setup", "write_internal", S("key:string:60", "value:any?")),
    ("build_playbook", "setup", "write_internal", S()), ("run_safety_test", "setup", "write_internal", S("only:array?")),
    ("rank_customers", "setup", "write_internal", S("goal:string:40?")),
    ("set_goals", "setup", "write_internal", S("goals:array")),
    ("setup_start", "setup", "write_internal", S("company:string:200", "website:string:500?", "description:string:1000?")),
    ("setup_add", "setup", "write_internal", S("path:string:500?", "url:string:500?")),
    ("setup_answer", "setup", "write_internal", S("qid:string:60", "answer:any")), ("setup_skip", "setup", "write_internal", S("qid:string:60")),
    ("setup_review", "setup", "read", S()), ("setup_build", "setup", "write_internal", S("goals:array?")),
    ("setup_request_turn_on", "setup", "write_internal", S()),
    ("pause_calling", "runtime", "write_internal", S("reason:string:300?")),
    ("call_customer", "runtime", "external", S("customer_id:integer", "mode:string:20?", "via:string:20?")),
    ("run_campaign", "runtime", "external", S("n:integer?", "mode:string:20?", "parallel:integer?", "goal:string:40?")),
    ("simulate_period", "runtime", "write_internal", S("days:integer?", "calls_per_day:integer?", "goals:array?", "cap_eur:number?")),
    ("run_followups", "runtime", "external", S("mode:string:20?")), ("resolve_handoff", "runtime", "write_internal", S("handoff_id:integer", "note:string:600?")),
    ("send_consent_requests", "runtime", "external", S("customer_ids:array?")), ("morning_run", "runtime", "external", S("mode:string:20?", "n:integer?")),
    # dangerous: only when PROVA_DEV=1 (reset / wipe / clock) or through an owner confirmation (erase)
    ("run_everything", "owner", "irreversible", S("sample:string:40?", "data_path:string:500?", "doc_paths:array?", "goal:string:600?",
                                                  "n_calls:integer?", "mode:string:20?")),
    ("load_sample", "owner", "irreversible", S("name:string:40")), ("reset_everything", "owner", "irreversible", S()),
    ("erase_customer", "runtime", "irreversible", S("customer_id:integer")),  # needs_human: the owner confirms with a token
    ("advance_clock", "owner", "write_internal", S("hours:number")), ("set_clock", "owner", "write_internal", S("iso:string:40")),
]

_REST = [
    _t("rest_read", ["mcp", "owner"], "read", S("method:string:10", "path:string:500"), rate="60/min", scope="read"),
    _t("rest_write", ["mcp", "owner"], "write_internal", S("method:string:10", "path:string:500"), rate="30/min", scope="setup"),
]


@lru_cache(maxsize=1)
def _build() -> dict:
    reg = {}
    for e in _gateway_entries() + _SETUP + _REST:
        reg[e["name"]] = e
    for name, scope, effect, schema in _MCP:
        # "owner" scope tools can never be called by role mcp (denied in check step 2); needs_human marks the irreversible ones.
        reg[name] = _t(name, ["mcp", "owner"], effect, schema, rate="60/min", needs_human=effect == "irreversible", scope=scope)
    return reg


def registry() -> dict:
    return _build()


def get(name: str):
    return registry().get(name)


def names(role: str | None = None) -> list:
    return [n for n, e in registry().items() if role is None or role in e["roles"]]
