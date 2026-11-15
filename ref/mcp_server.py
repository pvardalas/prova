"""Renewal Desk MCP server (stdio). Thin httpx client of the desk REST API."""
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("DESK_URL", "http://127.0.0.1:8765").rstrip("/")
mcp = FastMCP("renewal-desk")

DOWN = {"error": f"Renewal Desk server not reachable at {BASE}. Start it from the repo root with: uv run python -m desk.server"}


def _call(method, path, params=None, json=None, timeout=120):
    try:
        r = httpx.request(method, BASE + path, params=params, json=json, timeout=timeout)
    except httpx.TransportError:
        return DOWN
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}", "detail": data}
    return data


def _trim_call(c):
    c = dict(c)
    c.pop("audio_path", None)
    if isinstance(c.get("transcript"), list):
        c["transcript_lines"] = len(c["transcript"])
        c.pop("transcript")
    return c


GUIDE = """RENEWAL DESK (Helios Mobile) - overview for an AI agent

What it is: a desk that renews expiring consumer mobile contracts. An AI voice agent phones each customer,
knows their usage/complaints/history, and sells a renewal using only approved offers. The customer can ask for
a human, who is then placed in a human queue. Calls run in batches of 5. Unfinished business becomes follow-ups.
A human supervisor watches the same data in a web UI. You are using the same REST API as that UI.
(In this prototype the customers are simulated and time is a desk clock you can advance.)

Normal order of steps:
 1. desk_status - desk clock, counts, live calls, queue, pending follow-ups, active campaign.
 2. list_expiring_customers(window_days) - who is about to expire; list_offers - the approved catalogue.
 3. EITHER plan_campaign (the desk ranks customers and writes call briefs automatically; returns campaign_id)
    OR write briefs yourself with set_customer_brief per customer (after plan_campaign creates the campaign;
    your brief replaces the automatic one).
 4. start_call_batch(campaign_id, size=5) - starts up to 5 calls concurrently in the background; returns at once.
 5. Poll list_calls(campaign_id) / recent_events(after_id) until the calls are status 'ended'.
 6. campaign_report(campaign_id) - outcome counts, monthly revenue retained, pending follow-ups, queue, rule breaches.
 7. Repeat start_call_batch for the next 5. Handle follow-ups: list_followups; advance_clock(hours) to make
    them due; run_due_followups. Handle the human queue: human_queue -> pickup_handoff -> resolve_handoff.

Call outcomes: renewed / upgraded (sold; customer status renewed), callback (customer named a time; follow-up),
thinking (follow-up in 2 days), no_answer (retry in 4 hours, max 3 attempts), human (customer asked for a person;
goes to human queue with a context card), declined (follow-up 'last_chance' before contract end if the win chance
was >= 0.4, otherwise customer lost), do_not_call (customer must never be called again), failed (technical).

Rules (never break them): the agent discloses it is an AI and never denies it; a customer who asks for a human
gets one; 'stop calling me' means stop (do_not_call); only approved offers and never a discount above the
brief's max_discount_pct (itself <= the offer's ceiling). Never invent offers, prices or perks.
Never read or ask for the customers' private test persona; it is not exposed.
"""


@mcp.tool()
def desk_guide() -> str:
    """Read this first. Returns an overview of Renewal Desk: what it is, the normal order of steps,
    the meaning of each call outcome, and the rules every call and every agent must follow."""
    return GUIDE


@mcp.tool()
def desk_status() -> dict:
    """Snapshot of the desk: desk clock ('now'), customer counts by status (active/renewed/lost/do_not_call),
    calls currently live, human queue size, pending and due follow-ups, and the latest campaign. Start here
    and call it again whenever you need to know what is happening."""
    return _call("GET", "/api/status")


@mcp.tool()
def list_expiring_customers(window_days: int = 30) -> list | dict:
    """List active customers whose contract ends within `window_days` of the desk clock, soonest first.
    Each item has profile (plan, price, contract_end, usage, complaints, notes), the latest call brief
    (win_chance, recommended offer, max discount) if one exists, and last_outcome of their latest call.
    Use it to decide who to call. Use get_customer for full detail."""
    return _call("GET", "/api/customers", params={"expiring_days": window_days})


@mcp.tool()
def get_customer(customer_id: int) -> dict:
    """Full history of one customer: profile, latest brief, all calls (newest first, transcripts shortened
    to a line count), follow-ups and human-queue entries. Use before writing a brief or taking over a handoff."""
    d = _call("GET", f"/api/customers/{customer_id}")
    if isinstance(d, dict) and isinstance(d.get("calls"), list):
        d["calls"] = [_trim_call(c) for c in d["calls"]]
    return d


@mcp.tool()
def list_offers() -> list | dict:
    """The approved offer catalogue (id, name, monthly_price, data_gb, minutes, term_months, perks,
    max_discount_pct). The calling agent may only sell these and never discount beyond max_discount_pct.
    Offer ids are what you use in set_customer_brief."""
    return _call("GET", "/api/offers")


@mcp.tool()
def plan_campaign(window_days: int = 30, goal: str = "") -> dict:
    """Create a campaign: the desk ranks expiring customers by win chance and writes an automatic call brief
    for each. Returns the campaign (note its id) and the briefs. May take 10-60 seconds (it uses an LLM).
    window_days: contracts ending within this many days. goal: optional free text steering the briefs
    (e.g. 'protect revenue, avoid deep discounts'). Next: start_call_batch."""
    return _call("POST", "/api/campaigns", json={"window_days": window_days, "goal": goal}, timeout=300)


@mcp.tool()
def set_customer_brief(customer_id: int, win_chance: float | None = None, recommended_offer_id: str | None = None,
                       fallback_offer_id: str | None = None, max_discount_pct: int | None = None,
                       persistence: int | None = None, opening_hook: str | None = None,
                       likely_objection: str | None = None, talking_points: list[str] | None = None,
                       author: str = "agent") -> dict:
    """Write or override the call brief the voice agent will follow for one customer (use instead of, or
    after, plan_campaign). Only fields you pass are set.
    win_chance: 0..1 estimate that they renew (higher = called earlier). recommended_offer_id / fallback_offer_id:
    ids from list_offers. max_discount_pct: the most discount the caller may give this customer (must not exceed
    the offer's max_discount_pct). persistence: 0..3 objection rounds before a polite exit. opening_hook: first
    thing to say that shows we know them. likely_objection: what they will probably push back on.
    talking_points: short list of facts/arguments. author: your name, recorded on the brief."""
    fields = {k: v for k, v in dict(win_chance=win_chance, recommended_offer_id=recommended_offer_id,
              fallback_offer_id=fallback_offer_id, max_discount_pct=max_discount_pct, persistence=persistence,
              opening_hook=opening_hook, likely_objection=likely_objection, talking_points=talking_points,
              author=author).items() if v is not None}
    return _call("PUT", f"/api/customers/{customer_id}/brief", json=fields)


@mcp.tool()
def start_call_batch(campaign_id: int, size: int = 5, mode: str = "sim") -> dict:
    """Start the next batch of calls for a campaign. Returns immediately ({started: true}); the calls run in
    the background (about 1-3 minutes). Picks the next `size` (default 5) not-yet-called customers by win chance
    and calls them concurrently. mode 'sim' = simulated customers (default, use this). Afterwards poll
    list_calls(campaign_id) or recent_events until the calls have ended, then campaign_report."""
    return _call("POST", f"/api/campaigns/{campaign_id}/batch", json={"size": size, "mode": mode})


@mcp.tool()
def list_calls(campaign_id: int | None = None, status: str | None = None) -> list | dict:
    """List calls newest first WITHOUT transcripts: id, customer_name, status (queued/live/ended/failed), kind
    (first/followup/retry), attempt, duration_s, outcome, outcome_detail, summary. Filter by campaign_id and/or
    status. Use to watch a batch progress; use get_call for one call's transcript and review."""
    return _call("GET", "/api/calls", params={k: v for k, v in dict(campaign_id=campaign_id, status=status).items() if v is not None})


@mcp.tool()
def get_call(call_id: int, max_transcript_lines: int = 60) -> dict:
    """One call in full: outcome, outcome_detail, summary, the post-call review (objections, mood, whether the AI
    was disclosed, rule_breaches, confirmed_outcome, next_step) and the transcript (roles agent/customer/tool).
    max_transcript_lines keeps the LAST n lines (default 60; 0 = all)."""
    c = _call("GET", f"/api/calls/{call_id}")
    if isinstance(c, dict) and isinstance(c.get("transcript"), list):
        c.pop("audio_path", None)
        t = c["transcript"]
        if max_transcript_lines and len(t) > max_transcript_lines:
            c["transcript_truncated_from"] = len(t)
            c["transcript"] = t[-max_transcript_lines:]
    return c


@mcp.tool()
def campaign_report(campaign_id: int) -> dict:
    """Report for a campaign: counts per outcome, monthly revenue retained, pending follow-ups, human queue
    size and any rule breaches found by the reviewer. Read it after a batch ends. May take a few seconds."""
    return _call("GET", f"/api/campaigns/{campaign_id}/report", timeout=300)


@mcp.tool()
def list_followups() -> list | dict:
    """All follow-ups (pending first): id, customer_name, due_at (desk clock), reason (callback/thinking/
    no_answer/last_chance/human_requested), notes, status (pending/done/cancelled). Follow-ups only run
    once they are due; see advance_clock and run_due_followups."""
    return _call("GET", "/api/followups")


@mcp.tool()
def run_due_followups(mode: str = "sim") -> dict:
    """Start calls, in the background, for every follow-up whose due_at has passed on the desk clock.
    Returns immediately with the number due. Then poll list_calls / recent_events. If nothing is due,
    use advance_clock first."""
    return _call("POST", "/api/followups/run", json={"mode": mode})


@mcp.tool()
def advance_clock(hours: float) -> dict:
    """Move the desk clock forward by `hours` (simulated time, e.g. 4 for a no_answer retry, 48 for a
    'thinking' follow-up). Returns the new time and how many follow-ups are now due. Does not start calls."""
    return _call("POST", "/api/clock/advance", json={"hours": hours})


@mcp.tool()
def human_queue() -> list | dict:
    """Customers waiting for a human (they asked for one, or the call needed one). Each item: id (queue id),
    customer_name, customer_phone, reason, status (waiting/picked_up/resolved) and a context_card (summary,
    offer on the table, objections, customer mood, suggested next step) so the human can continue without
    asking the customer to repeat anything."""
    return _call("GET", "/api/queue")


@mcp.tool()
def pickup_handoff(queue_id: int, by: str = "agent") -> dict:
    """Claim a human-queue item so nobody else handles it (status picked_up). queue_id comes from human_queue.
    by: who is taking it (your name)."""
    return _call("POST", f"/api/queue/{queue_id}/pickup", json={"by": by})


@mcp.tool()
def resolve_handoff(queue_id: int, resolution: str, followup_in_hours: float | None = None) -> dict:
    """Close a human-queue item after handling it. resolution: what was done/agreed (free text, be specific).
    followup_in_hours: optional; also schedule an AI follow-up call that many desk-clock hours from now."""
    payload = {"resolution": resolution}
    if followup_in_hours is not None:
        payload["followup_in_hours"] = followup_in_hours
    return _call("POST", f"/api/queue/{queue_id}/resolve", json=payload)


@mcp.tool()
def recent_events(after_id: int = 0, limit: int = 50) -> dict:
    """Activity feed (call_started, transcript, tool, call_ended, review, followup, handoff, campaign, clock).
    Pass after_id=0 for the oldest, or the 'last_id' from a previous call to get only new events. Returns
    {events, last_id}. Transcript lines are trimmed. Use it to poll progress without re-reading everything."""
    ev = _call("GET", "/api/events", params={"after": after_id, "limit": 500})
    if isinstance(ev, dict):
        return ev
    if after_id == 0 and len(ev) > limit:
        ev = ev[-limit:]
    else:
        ev = ev[:limit]
    for e in ev:
        p = e.get("payload")
        if isinstance(p, dict) and isinstance(p.get("text"), str) and len(p["text"]) > 200:
            p["text"] = p["text"][:200] + "..."
    return {"events": ev, "last_id": ev[-1]["id"] if ev else after_id}


if __name__ == "__main__":
    # default stdio (Claude Code, .mcp.json). MCP_TRANSPORT=streamable-http serves http://127.0.0.1:8766/mcp
    # for agents that connect by URL (MCP_HOST / MCP_PORT to change).
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("MCP_PORT", 8766))
    mcp.run(transport=transport)
