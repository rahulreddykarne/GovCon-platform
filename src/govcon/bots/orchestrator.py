"""Run the bots in order and refuse to call a failed opportunity finished."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from govcon.bots.store import begin_run, finish_run
from govcon.bots.workflows import (
    execute_alert,
    execute_amendment,
    execute_awards,
    execute_bid,
    execute_compliance,
    execute_discovery,
    execute_document,
    execute_matching,
    execute_operations,
)
from govcon.config import Settings
from govcon.logging import redact
from govcon.models import BotRun, Task
from govcon.tasks import queue


def queue_orchestrator(
    session: Session,
    *,
    settings: Settings,
    slot: str,
    pull: bool,
    trigger: str,
) -> tuple[Task, bool]:
    """Queue one orchestrator task. The same slot and pull flag are not queued twice."""
    return queue.enqueue(
        session,
        task_type="bot_run",
        opportunity_id=None,
        input_revision={"bot": "orchestrator", "slot": slot, "pull": pull},
        payload={"bot_name": "orchestrator", "slot": slot, "pull": pull, "trigger": trigger},
        settings=settings,
    )


def execute_orchestrator(
    session: Session,
    settings: Settings,
    *,
    trigger: str,
    slot: str,
    pull: bool,
    persist_start: bool = False,
) -> BotRun:
    run, started = begin_run(
        session, bot_name="orchestrator", idempotency_key=f"orchestrator:{slot}:pull={int(pull)}",
        trigger=trigger, inputs={"slot": slot, "pull": pull},
    )
    if not started:
        return run
    if persist_start:
        # The row is visible as running before the network work. A killed worker
        # leaves that row for the next start to mark failed.
        session.commit()
    try:
        discovery = execute_discovery(
            session, settings, slot=slot, trigger="orchestrator", parent_run_id=run.id, pull=pull,
        )
        states: dict[str, str] = {}
        children = [_child(discovery)]
        if discovery.status != "failed":
            for opportunity_id in (discovery.outputs or {}).get("opportunity_ids") or []:
                state, extra = _one_opportunity(session, settings, int(opportunity_id), run.id)
                states[str(opportunity_id)] = state
                children.extend(extra)
        alert = execute_alert(session, settings, slot=slot, trigger="orchestrator", parent_run_id=run.id)
        operations = execute_operations(session, settings, slot=slot, trigger="orchestrator", parent_run_id=run.id)
        children.extend([_child(alert), _child(operations)])
        incomplete = discovery.status == "failed" or any(value == "incomplete" for value in states.values())
        if alert.status == "failed":
            incomplete = True
        finish_run(run, "completed_with_errors" if incomplete else "succeeded", outputs={
            "children": children,
            "opportunity_state": states,
            "any_incomplete": incomplete,
            "state": "incomplete" if incomplete else "needs_decision",
        })
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"any_incomplete": True, "state": "incomplete"})
    return run


def _one_opportunity(session: Session, settings: Settings, opportunity_id: int, parent_id: int) -> tuple[str, list[dict[str, Any]]]:
    document = execute_document(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
    matching = execute_matching(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
    compliance = execute_compliance(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
    awards = execute_awards(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
    amendment = execute_amendment(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
    ran = [document, matching, compliance, awards, amendment]
    matched = bool((matching.outputs or {}).get("matched"))
    blocked = document.status == "failed" or compliance.status == "failed" or matching.status == "failed"
    if matched and not blocked:
        bid = execute_bid(session, settings, opportunity_id=opportunity_id, trigger="orchestrator", parent_run_id=parent_id)
        ran.append(bid)
        if bid.status == "failed":
            state = "incomplete"
        else:
            state = "needs_decision"
    elif blocked:
        state = "incomplete"
    elif not matched:
        state = "not_a_match"
    else:
        state = "incomplete"
    return state, [_child(item) for item in ran]


def _child(run: BotRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "bot": run.bot_name,
        "status": run.status,
        "opportunity_id": run.opportunity_id,
        "state": (run.outputs or {}).get("state"),
        "attempt": run.attempt,
    }


def new_slot() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
