"""Mission-control pages. Every number comes from a stored row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.bots.catalog import CATALOG
from govcon.config import Settings
from govcon.models import BotApproval, BotRun, MarketPriceRun, Match, Opportunity
from govcon.operating.integrations import MARKET_PRICE_CARD, integration_cards

LA = ZoneInfo("America/Los_Angeles")

# The diagram is the runtime order in the orchestrator, not a sample story.
ARCHITECTURE: list[dict[str, Any]] = [
    {
        "label": "01 / Federal data sources",
        "layout": "sources",
        "nodes": [
            {"id": "sam", "kind": "Source", "title": "SAM.gov", "caption": "Opportunities", "tip": "SAM.gov opportunity notices"},
            {"id": "dibbs", "kind": "Source", "title": "DIBBS", "caption": "RFQs and PDFs", "tip": "DLA DIBBS RFQ notices and files"},
            {"id": "usaspending", "kind": "Source", "title": "USAspending", "caption": "Award context", "tip": "USAspending historical awards"},
        ],
    },
    {
        "label": "02 / Coordination",
        "layout": "center",
        "nodes": [
            {"id": "orchestrator", "kind": "Orchestration", "title": "Orchestrator", "caption": "Routes work and records every step", "tip": "Coordinates each stage and keeps a failed step incomplete"},
        ],
    },
    {
        "label": "03 / Specialist agents",
        "layout": "flow",
        "nodes": [
            {"id": "discovery", "kind": "Agent", "title": "Discovery", "caption": "Find changes", "tip": "Tracks new notices and changes"},
            {"id": "document", "kind": "Agent", "title": "Document", "caption": "Extract requirements", "tip": "Reads files and cites passages"},
            {"id": "matching", "kind": "Agent", "title": "Matching", "caption": "Explain fit", "tip": "Compares a notice with the watchlist"},
            {"id": "compliance", "kind": "Agent", "title": "Compliance", "caption": "Risks and gaps", "tip": "Eligibility, clauses, deadlines, unanswered questions"},
            {"id": "market_prices", "kind": "Agent", "title": "Market prices", "caption": "Web prices, estimated cost", "tip": "For a pursued product, Claude searches commercial sites for up to five prices. Government sites are excluded."},
            {"id": "bid_decision", "kind": "Agent", "title": "Decision", "caption": "Bid or no-bid", "tip": "Prepares a recommendation. A person decides."},
        ],
    },
    {
        "label": "04 / Models and the human gate",
        "layout": "branches",
        "nodes": [
            {"id": "deepseek", "kind": "Model", "title": "DeepSeek", "caption": "Analysis", "tip": "Reads solicitation text only when the gateway allows it"},
            {"id": "claude_web", "kind": "Model", "title": "Claude web search", "caption": "Commercial prices", "tip": "Anthropic web search and fetch, only when the gateway allows the opportunity's data"},
            {"id": "jev", "kind": "Model", "title": "JEV", "caption": "Recommendation", "tip": "Structured decision package when policy and a key allow it"},
            {"id": "rules", "kind": "Fallback", "title": "Rules engine", "caption": "Hard checks", "tip": "Used when JEV does not run. The reason is stored."},
            {"id": "human", "kind": "Approval", "title": "You decide", "caption": "Pursue or no-bid", "tip": "Pursuit, submission, and external messages stay human"},
        ],
    },
    {
        "label": "05 / After the recommendation",
        "layout": "branches",
        "nodes": [
            {"id": "amendment", "kind": "Agent", "title": "Amendments", "caption": "What changed", "tip": "Source events and their operational impact"},
            {"id": "alert", "kind": "Agent", "title": "Alerts", "caption": "What needs you", "tip": "Outbox digests. No email from this agent."},
            {"id": "awards", "kind": "Agent", "title": "Awards", "caption": "History, not a promise", "tip": "Stored USAspending comps"},
            {"id": "operations", "kind": "Agent", "title": "Operations", "caption": "Is it working", "tip": "Heartbeats, retries, and freshness from local rows"},
        ],
    },
]

_BOT_FOR_NODE = {
    "orchestrator": "orchestrator",
    "discovery": "discovery",
    "document": "document",
    "matching": "matching",
    "compliance": "compliance",
    "bid_decision": "bid_decision",
    "amendment": "amendment",
    "alert": "alert",
    "awards": "awards",
    "operations": "operations",
}


def overview(session: Session, settings: Settings) -> dict[str, Any]:
    pending = list(session.scalars(
        select(BotApproval).where(BotApproval.status == "pending").order_by(BotApproval.requested_at.desc()).limit(8)
    ).all())
    running = session.scalar(
        select(func.count()).select_from(BotRun).where(
            BotRun.status == "running",
            BotRun.started_at >= datetime.now(UTC) - timedelta(minutes=15),
        )
    ) or 0
    new_matches = session.scalar(
        select(func.count()).select_from(Match).where(Match.status == "new", Match.active.is_(True))
    ) or 0
    activity = list(session.scalars(
        select(BotRun).order_by(BotRun.started_at.desc()).limit(6)
    ).all())
    from govcon.company.strategy import missing_labels
    from govcon.workflow.app_settings import COMPANY_STRATEGY, get_setting

    cards = integration_cards(session, settings)
    healthy = sum(1 for card in cards if card["status"] == "Healthy")
    attention = [card for card in cards if card["status"] in {"Failed", "Degraded", "Blocked by policy"}][:4]
    return {
        "greeting": _greeting(),
        "pending": [_approval(session, row) for row in pending],
        "pending_count": len(pending) if len(pending) < 8 else _pending_total(session),
        "running_count": int(running),
        "new_match_count": int(new_matches),
        "activity": [_run_line(row) for row in activity],
        "current": _current(session, pending),
        "healthy_count": healthy,
        "check_count": len(cards),
        "attention": attention,
        "strategy_missing": missing_labels(get_setting(session, COMPANY_STRATEGY)),
    }


def agent_board(session: Session, selected: str | None) -> dict[str, Any]:
    latest = _latest_by_bot(session)
    cards = []
    for name, spec in CATALOG.items():
        run = latest.get(name)
        cards.append({
            "id": name,
            "title": spec["title"],
            "initials": "".join(part[0] for part in spec["title"].split()[:2]).upper(),
            "description": spec["outputs"],
            "status": _run_status(run),
            "tag": _run_tag(run),
            "foot": _run_foot(run),
        })
    chosen = selected if isinstance(selected, str) and selected in CATALOG else "orchestrator"
    return {"cards": cards, "selected": _node_detail(session, chosen, latest)}


def architecture_board(session: Session, settings: Settings, selected: str | None) -> dict[str, Any]:
    latest = _latest_by_bot(session)
    cards = {card["name"]: card for card in integration_cards(session, settings)}
    cards[_MARKET_NODE] = _market_prices_node(session)
    rows = []
    known = set()
    for band in ARCHITECTURE:
        nodes = []
        for node in band["nodes"]:
            known.add(node["id"])
            nodes.append({**node, "status": _node_status(node["id"], latest, cards), "tag": _node_tag(node["id"], latest, cards)})
        rows.append({**band, "nodes": nodes})
    chosen = selected if isinstance(selected, str) and selected in known else "orchestrator"
    return {"rows": rows, "selected": _node_detail(session, chosen, latest, cards)}


_MARKET_NODE = "market_prices"
# Architecture nodes whose health is an integration card.
_CARD_FOR_NODE = {"sam": "SAM.gov", "dibbs": "DIBBS", "usaspending": "USAspending", "deepseek": "DeepSeek", "jev": "JEV",
                  "claude_web": MARKET_PRICE_CARD, _MARKET_NODE: _MARKET_NODE}
_MARKET_RUN_TAGS = {"completed": "good", "no_results": "info", "skipped": "info", "blocked": "warn", "failed": "bad"}


def _market_prices_node(session: Session) -> dict[str, Any]:
    """The newest stored web price search, as a card-shaped row for the diagram."""
    from govcon.sourcing.market_prices import run_summary

    run = session.scalar(select(MarketPriceRun).order_by(MarketPriceRun.created_at.desc(), MarketPriceRun.id.desc()).limit(1))
    if run is None:
        return {"status": "No run", "tag": "info", "blurb": "No web price search is stored yet.", "rows": [], "run": None}
    return {
        "status": run.status.replace("_", " "),
        "tag": _MARKET_RUN_TAGS.get(run.status, "info"),
        "blurb": run_summary(run),
        "rows": [{"label": "Opportunity", "value": f"#{run.opportunity_id}"}],
        "run": {"status": run.status.replace("_", " "), "when": _local(run.created_at), "detail": run_summary(run)[:240]},
    }


def _node_detail(session: Session, node_id: str, latest: dict[str, BotRun], cards: dict | None = None) -> dict[str, Any]:
    if node_id == _MARKET_NODE:
        node = (cards or {}).get(_MARKET_NODE) or _market_prices_node(session)
        return {
            "id": node_id,
            "title": "Market prices",
            "trigger": "Preparation runs it after compliance for every pursued product opportunity. "
                       "The Products tab can search again.",
            "inputs": "Product description, NSN, part number, quantity, unit and specifications from the "
                      "solicitation summary and requirements.",
            "outputs": "Up to five priced web listings with links, and an estimated cost: the median of the listings "
                       "that match the specification. Margin math uses it until a supplier quote is recorded.",
            "permissions": "The product description goes to Anthropic only when the gateway allows the opportunity's "
                           "data class. Government sites are excluded. Web prices are never treated as quotes.",
            "failure": "A skipped, blocked or failed search is stored with its reason. Preparation continues and "
                       "margin math uses no estimate.",
            "run": node["run"],
            "rows": node["rows"],
        }
    bot_name = _BOT_FOR_NODE.get(node_id)
    if bot_name and bot_name in CATALOG:
        spec = CATALOG[bot_name]
        run = latest.get(bot_name)
        return {
            "id": node_id,
            "title": spec["title"],
            "trigger": spec["trigger"],
            "inputs": spec["inputs"],
            "outputs": spec["outputs"],
            "permissions": spec["permissions"],
            "failure": spec["failure"],
            "run": _run_line(run) if run is not None else None,
        }
    static = {
        "sam": ("SAM.gov", "The morning and evening ingest chains call the SAM opportunities API when a key is configured."),
        "dibbs": ("DIBBS", "Those chains read the public daily index, then download RFQ PDFs for notices just stored."),
        "usaspending": ("USAspending", "The 7:00 AM chain stores award history. It is context, not a promised price."),
        "deepseek": ("DeepSeek", "Structured analysis runs only through the classification gateway. Sparse output is stored as incomplete."),
        "jev": ("JEV", "Bid/no-bid uses JEV when the package is allowed. Otherwise the rules engine runs and the reason is stored."),
        "rules": ("Rules engine", "Deterministic checks. They remain the result when JEV does not run."),
        "human": ("Human authority", "Approving a bot recommendation records the person. It does not submit a bid, send email, or change AI sharing."),
        "claude_web": ("Claude web search", "The market prices step calls Anthropic web search and fetch for a pursued product. At most five prices are kept; government sites are excluded."),
    }
    title, body = static.get(node_id, (node_id, "No definition is stored for this node."))
    card = (cards or {}).get(_CARD_FOR_NODE.get(node_id, ""))
    return {
        "id": node_id,
        "title": title,
        "trigger": body,
        "inputs": card["blurb"] if card else "See the definition above.",
        "outputs": card["status"] if card else "No stored health row for this node.",
        "permissions": "External calls stay behind the classification gateway.",
        "failure": "A missing run is shown as missing.",
        "run": None,
        "rows": card["rows"] if card else [],
    }


def _approval(session: Session, row: BotApproval) -> dict[str, Any]:
    title = row.summary
    if row.opportunity_id:
        opp = session.get(Opportunity, row.opportunity_id)
        if opp is not None and opp.title:
            title = opp.title
    return {
        "id": row.id,
        "title": title,
        "kind": row.kind,
        "summary": row.summary,
        "opportunity_id": row.opportunity_id,
    }


def _current(session: Session, pending: list[BotApproval]) -> dict[str, Any] | None:
    for row in pending:
        if row.opportunity_id:
            opp = session.get(Opportunity, row.opportunity_id)
            if opp is not None:
                return {
                    "title": opp.title or opp.source_id,
                    "opportunity_id": opp.id,
                    "why": row.summary,
                    "next_action": "Review the cited evidence, then record a decision on Bots. That does not submit a bid.",
                }
    match = session.scalar(
        select(Match).where(Match.status == "new", Match.active.is_(True)).order_by(Match.rank_score.desc().nullslast(), Match.id).limit(1)
    )
    if match is None:
        return None
    opp = session.get(Opportunity, match.opportunity_id)
    if opp is None:
        return None
    return {
        "title": opp.title or opp.source_id,
        "opportunity_id": opp.id,
        "why": "This is the highest stored rank among new matches. Rank is not a decision.",
        "next_action": "Open the notice. Analysis that has not run is labeled on the inbox.",
    }


def _pending_total(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(BotApproval).where(BotApproval.status == "pending")) or 0)


def _latest_by_bot(session: Session) -> dict[str, BotRun]:
    rows = session.scalars(select(BotRun).order_by(BotRun.started_at.desc(), BotRun.id.desc()).limit(200)).all()
    found: dict[str, BotRun] = {}
    for row in rows:
        found.setdefault(row.bot_name, row)
    return found


def _run_line(run: BotRun) -> dict[str, Any]:
    outputs = run.outputs or {}
    detail = run.error or outputs.get("state") or run.status
    return {
        "bot": run.bot_name,
        "status": run.status,
        "when": _local(run.finished_at or run.started_at),
        "detail": str(detail)[:240],
        "opportunity_id": run.opportunity_id,
    }


def _run_status(run: BotRun | None) -> str:
    if run is None:
        return "No run"
    return run.status.replace("_", " ")


def _run_tag(run: BotRun | None) -> str:
    if run is None:
        return "info"
    if run.status in {"failed", "blocked"} or (run.outputs or {}).get("state") == "incomplete":
        return "bad"
    if run.status in {"waiting_approval", "completed_with_errors", "running"}:
        return "warn"
    if run.status == "succeeded":
        return "good"
    return "info"


def _run_foot(run: BotRun | None) -> str:
    if run is None:
        return "No stored run"
    return _local(run.finished_at or run.started_at)


def _node_status(node_id: str, latest: dict[str, BotRun], cards: dict[str, dict]) -> str:
    bot_name = _BOT_FOR_NODE.get(node_id)
    if bot_name:
        return _run_status(latest.get(bot_name))
    card = cards.get(_CARD_FOR_NODE.get(node_id, ""))
    if card:
        return card["status"]
    if node_id == "rules":
        return "Local"
    if node_id == "human":
        return "Required"
    return "No run"


def _node_tag(node_id: str, latest: dict[str, BotRun], cards: dict[str, dict]) -> str:
    bot_name = _BOT_FOR_NODE.get(node_id)
    if bot_name:
        return _run_tag(latest.get(bot_name))
    card = cards.get(_CARD_FOR_NODE.get(node_id, ""))
    if card:
        return card["tag"]
    return "info"


def _greeting() -> str:
    hour = datetime.now(LA).hour
    if hour < 12:
        salute = "Good morning"
    elif hour < 17:
        salute = "Good afternoon"
    else:
        salute = "Good evening"
    return f"{salute}. Here is what needs you."


def _local(value: datetime | None) -> str:
    if value is None:
        return "time not stored"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(LA).strftime("%Y-%m-%d %I:%M %p PT")
