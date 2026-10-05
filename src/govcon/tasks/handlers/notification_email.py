"""``notification_email``: deliver one in-app notification by email (ADR-070).

The SMTP send happens with no transaction open and reuses the digest's TLS
rules and error redaction. Retries are bounded by the task queue; when they
run out the delivery is marked failed and the task appears on /ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from sqlalchemy.orm import Session

from govcon.models import Notification, NotificationDelivery, Opportunity, Task
from govcon.tasks.errors import TaskCancelled
from govcon.tasks.registry import Step, StepContext, TaskHandler, register

NOTIFICATION_EMAIL_TASK = "notification_email"
_SUBJECTS = {
    "review_assigned": "You have been assigned a review",
    "review_reminder": "Reminder: a review is waiting for you",
    "review_overdue_escalation": "A review is overdue",
    "deadline_escalation": "Deadline approaching without a bid decision",
    "auto_pursued": "An opportunity was pursued automatically",
    "registration_expiring": "SAM registration is expiring",
    "outcome_suggested": "An award may match a submitted bid",
}


@dataclass
class _Message:
    delivery_id: int
    to: str
    subject: str
    plain: str
    html: str


def _prepare(session: Session, task: Task, ctx: StepContext) -> _Message:
    delivery = session.get(NotificationDelivery, ctx.payload["delivery_id"])
    if delivery is None or delivery.status == "sent":
        raise TaskCancelled("the notification was already delivered or removed")
    notification = session.get(Notification, delivery.notification_id)
    if notification is None:
        raise TaskCancelled("the notification was removed before it could be emailed")
    delivery.attempts += 1
    opportunity = session.get(Opportunity, notification.opportunity_id) if notification.opportunity_id else None
    kind = notification.notification_type
    subject = f"GovCon: {_SUBJECTS.get(kind, kind.replace('_', ' ').capitalize())}"
    lines = [subject.removeprefix("GovCon: ") + "."]
    if opportunity is not None:
        lines.append(f"Opportunity: {opportunity.title} (#{opportunity.id})")
    for key, value in (notification.payload or {}).items():
        if isinstance(value, (str, int, float)) and key not in ("title",):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    lines.append("Open GovCon to act on it. You can acknowledge it on the Notifications page.")
    plain = "\n".join(lines)
    html = "<p>" + "</p><p>".join(escape(line) for line in lines) + "</p>"
    return _Message(delivery.id, delivery.recipient, subject, plain, html)


def _execute(message: _Message, ctx: StepContext) -> _Message:
    from govcon.alerts.digest import send_smtp

    send_smtp(ctx.settings, subject=message.subject, html=message.html, plain=message.plain, to=message.to)
    return message


def _publish(session: Session, task: Task, message: _Message, ctx: StepContext) -> None:
    delivery = session.get(NotificationDelivery, message.delivery_id)
    if delivery is None:
        raise TaskCancelled("the notification delivery was removed before it could be recorded")
    delivery.status = "sent"
    delivery.sent_at = datetime.now(UTC)
    delivery.error = None
    ctx.result = {"delivery_id": delivery.id}


def _on_failed(session: Session, task: Task) -> None:
    delivery = session.get(NotificationDelivery, (task.payload or {}).get("delivery_id"))
    if delivery is not None and delivery.status != "sent":
        delivery.status = "failed"
        delivery.error = task.last_error


register(TaskHandler(
    task_type=NOTIFICATION_EMAIL_TASK,
    steps=[Step(name="send", prepare=_prepare, execute=_execute, publish=_publish, timeout_seconds=120)],
    on_failed=_on_failed,
))
