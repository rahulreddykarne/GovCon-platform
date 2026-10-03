"""How a step's exception becomes a task outcome (roadmap §4.2 req. 6).

Waiting for input, waiting for budget, retrying and failed are distinct, and
every blocked or failed task names an owner and a next action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from govcon.models import Task


class TaskBlocked(Exception):
    """The task cannot continue until a person acts or budget is available."""

    def __init__(self, reason: str, *, status: str = "waiting_for_input", owner_role: str = "approver",
                 next_action: str, resume_at: datetime | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.owner_role = owner_role
        self.next_action = next_action
        self.resume_at = resume_at


class TaskCancelled(Exception):
    """The work no longer applies (e.g. the approval it served was withdrawn)."""

    def __init__(self, reason: str, *, superseded_by: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.superseded_by = superseded_by


class TaskSuperseded(Exception):
    """The inputs changed; replace this task with one for the current inputs.

    The replacement is queued and this task cancelled in one commit.
    """

    def __init__(self, reason: str, *, input_revision: dict, payload: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.input_revision = input_revision
        self.payload = payload


class TaskFailedPermanently(Exception):
    """Retrying cannot help; fail now and tell the owner what to fix."""

    def __init__(self, reason: str, *, owner_role: str = "owner", next_action: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.owner_role = owner_role
        self.next_action = next_action


def require_task_opportunity(task: Task) -> int:
    """Return the opportunity id for a handler that cannot run without one."""
    opportunity_id = task.opportunity_id
    if opportunity_id is None:
        raise TaskFailedPermanently(
            "task has no opportunity id",
            next_action="Cancel this task; it cannot run without an opportunity.",
        )
    return opportunity_id


@dataclass(frozen=True)
class Outcome:
    kind: str  # "block" | "cancel" | "supersede" | "fail" | "retry"
    exc: BaseException
    status: str | None = None
    owner_role: str | None = None
    next_action: str | None = None
    resume_at: datetime | None = None
    superseded_by: int | None = None


_BUDGET_NEXT = (
    "This opportunity's AI budget is a lifetime total and does not refill by waiting. Raise "
    "AI_MAX_INPUT_TOKENS_PER_OPPORTUNITY (or AI_MAX_COST_USD_PER_OPPORTUNITY) and restart the worker; "
    "the task then resumes automatically. A person can also retry or cancel it on /ops."
)


def classify(exc: BaseException) -> Outcome:
    from govcon.ai.budget import AIBudgetExceeded
    from govcon.ai.gateway import AIGatewayBlocked
    from govcon.ai.structured import StructuredCallError
    from govcon.compliance.pipeline import CompanyFactsInvalid

    if isinstance(exc, TaskBlocked):
        return Outcome("block", exc, exc.status, exc.owner_role, exc.next_action, exc.resume_at)
    if isinstance(exc, TaskCancelled):
        return Outcome("cancel", exc, superseded_by=exc.superseded_by)
    if isinstance(exc, TaskSuperseded):
        return Outcome("supersede", exc)
    if isinstance(exc, TaskFailedPermanently):
        return Outcome("fail", exc, owner_role=exc.owner_role, next_action=exc.next_action)
    if isinstance(exc, AIBudgetExceeded):
        return Outcome("block", exc, "waiting_for_budget", "owner", _BUDGET_NEXT)
    if isinstance(exc, CompanyFactsInvalid):
        return Outcome("fail", exc, owner_role="owner",
                       next_action="Fix the JSON in the file COMPANY_FACTS_PATH names, then retry the task.")
    if isinstance(exc, AIGatewayBlocked):
        return Outcome("block", exc, "waiting_for_input", "owner", _POLICY_NEXT)
    if isinstance(exc, StructuredCallError):
        if exc.reason == "budget_exceeded":
            return Outcome("block", exc, "waiting_for_budget", "owner", _BUDGET_NEXT)
        if exc.reason == "blocked_by_policy":
            return Outcome("block", exc, "waiting_for_input", "owner", _POLICY_NEXT)
        if exc.reason == "no_provider":
            return Outcome("block", exc, "waiting_for_input", "owner",
                           "Configure an AI provider key, then retry.")
        if exc.reason in ("schema_error", "render_error"):
            return Outcome("fail", exc, owner_role="owner",
                           next_action="Fix the prompt registry entry for this task, then retry.")
    return Outcome("retry", exc)


_POLICY_NEXT = (
    "The data-classification policy blocks this AI call. An owner must deliberately authorize "
    "the provider for this data class, then retry."
)
