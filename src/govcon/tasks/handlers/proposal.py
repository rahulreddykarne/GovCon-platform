"""``proposal_generation``: draft outside a transaction, publish under lock (ADR-062).

One step, three phases:

- prepare: under the opportunity lock, re-check the approval, package
  freshness and missing artifacts; resolve the drafting call.
- execute: call the AI provider with no transaction open (or build the
  placeholder draft when no provider is configured or a person asked for one).
- publish: under the lock again, recheck every input; store the proposal
  version and the submission package together, so a failure leaves neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.structured import PreparedCall, execute_prepared_call, persist_structured_result
from govcon.db import shared_session_factory
from govcon.models import Task, User
from govcon.tasks.errors import TaskBlocked, TaskCancelled, TaskSuperseded
from govcon.tasks.registry import Step, StepContext, TaskHandler, register
from govcon.workflow.proposal_generation import (
    PROPOSAL_TASK,
    GenerationNotAllowed,
    check_generation_allowed,
    generation_inputs,
)

_STALE_PACKAGE_ACTION = (
    "Regenerate the AI decision package (govcon decision run-package), re-approve the bid, "
    "and generation will be queued again."
)


@dataclass
class _Draft:
    inputs: dict[str, Any]
    prepared: PreparedCall | None  # None means use the placeholder draft
    executed: Any = None


def _current_inputs(session: Session, task: Task, ctx: StepContext) -> dict[str, Any]:
    """Check generation is still allowed and return the inputs as they are now."""
    try:
        review = check_generation_allowed(session, task.opportunity_id)
    except GenerationNotAllowed as exc:
        if exc.kind == "stale_package":
            raise TaskBlocked(str(exc), owner_role="approver", next_action=_STALE_PACKAGE_ACTION) from exc
        raise TaskCancelled(str(exc)) from exc
    current = generation_inputs(
        session, task.opportunity_id, review, without_ai=bool(ctx.payload.get("without_ai"))
    )
    if current != ctx.input_revision:
        raise TaskSuperseded(
            "the source, approval or commercial facts changed after this generation was queued; "
            "re-queued for the current inputs",
            input_revision=current,
        )
    return current


def _prepare(session: Session, task: Task, ctx: StepContext) -> _Draft:
    from govcon.ai.providers import provider_available
    from govcon.proposals.drafting import prepare_draft_call

    inputs = _current_inputs(session, task, ctx)
    if inputs["without_ai"] or not provider_available(ctx.settings):
        return _Draft(inputs=inputs, prepared=None)
    return _Draft(
        inputs=inputs,
        prepared=prepare_draft_call(session, opportunity_id=task.opportunity_id, settings=ctx.settings),
    )


def _execute(draft: _Draft, ctx: StepContext) -> _Draft:
    if draft.prepared is not None:
        engine = shared_session_factory(ctx.settings).kw["bind"]
        draft.executed = execute_prepared_call(draft.prepared, settings=ctx.settings, engine=engine)
    return draft


def _publish(session: Session, task: Task, draft: _Draft, ctx: StepContext) -> None:
    from govcon.proposals.drafting import draft_output
    from govcon.proposals.service import _build_placeholder_draft, publish_generated_proposal
    from govcon.submissions.service import generate_submission_package

    _current_inputs(session, task, ctx)  # nothing changed while drafting
    actor = session.get(User, task.created_by_user_id) if task.created_by_user_id else None
    if draft.executed is not None:
        draft_result = draft_output(persist_structured_result(session, draft.prepared, draft.executed))
        provider, model = draft_result.get("provider"), draft_result.get("model")
    else:
        draft_result = _build_placeholder_draft(session, task.opportunity_id)
        provider, model = "placeholder", None
    generated = publish_generated_proposal(
        session, opportunity_id=task.opportunity_id, actor=actor, draft_result=draft_result,
        provider=provider, model=model, settings=ctx.settings,
    )
    package = generate_submission_package(session, opportunity_id=task.opportunity_id, actor=actor)
    ctx.result = {
        "proposal_id": generated["proposal_id"],
        "version_id": generated["version_id"],
        "provider": provider,
        "submission_id": package.get("submission_id"),
    }


register(TaskHandler(
    task_type=PROPOSAL_TASK,
    steps=[Step(name="generate", prepare=_prepare, execute=_execute, publish=_publish, timeout_seconds=900)],
))
