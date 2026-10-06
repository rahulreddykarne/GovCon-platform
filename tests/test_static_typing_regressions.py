"""Behavioral edges uncovered while clearing the lint/typecheck debt."""

import pytest
from test_preparation import client as client
from test_preparation import db as db

from govcon.ai.structured import StructuredCallError, checked_output
from govcon.compliance.schemas import (
    RequirementExtractionV1,
    RequirementReconciliationV1,
)


def test_consumer_refuses_a_different_registered_output_schema():
    output = RequirementExtractionV1()
    assert checked_output(output, RequirementExtractionV1) is output
    with pytest.raises(StructuredCallError, match="schema_error: expected RequirementReconciliationV1"):
        checked_output(output, RequirementReconciliationV1)


def test_missing_budget_reservation_stops_settlement(upgraded_engine):
    from govcon.ai.budget import Reservation

    reservation = Reservation(upgraded_engine, -1, None, None)
    with pytest.raises(RuntimeError, match="usage reservation no longer exists"):
        reservation.finish()


@pytest.mark.parametrize("field", ["email", "display_name", "password", "role"])
def test_user_invitation_rejects_uploaded_text_fields(db, client, field):
    from uuid import uuid4

    from sqlalchemy import select
    from test_web_ui import _make_user

    from govcon.collaboration.users import create_session
    from govcon.models import User

    actor, _ = _make_user(db, f"invite-owner-{uuid4().hex}@regression.test", "owner")
    client.cookies.set("govcon_session", create_session(db, actor))
    db.commit()
    email = f"invite-target-{uuid4().hex}@regression.test"
    fields = {"email": email, "display_name": "Synthetic invite", "password": "SyntheticInvitePassword123!", "role": "reviewer"}
    del fields[field]
    response = client.post("/admin/users/invite", data=fields, files={field: ("unexpected.txt", b"invalid field")})
    assert response.status_code == 400
    assert f"{field} must be text" in response.text
    assert db.scalar(select(User).where(User.email == email)) is None


def test_orphaned_preparation_is_cancelled_without_processing(db):
    from govcon.models import Task
    from govcon.tasks.queue import enqueue
    from govcon.tasks.worker import run_once

    task, _ = enqueue(db, task_type="opportunity_preparation", opportunity_id=None,
                      input_revision={"source_revision": "synthetic orphan"})
    db.commit()
    run_once(task_id=task.id)
    db.expire_all()
    assert db.get(Task, task.id).status == "cancelled"


def test_removed_email_delivery_is_cancelled_at_publication(db):
    from types import SimpleNamespace

    from govcon.models import Task
    from govcon.tasks.errors import TaskCancelled
    from govcon.tasks.handlers.notification_email import _Message, _publish

    message = _Message(-1, "synthetic@regression.test", "Synthetic", "Synthetic", "<p>Synthetic</p>")
    with pytest.raises(TaskCancelled, match="removed"):
        _publish(db, Task(), message, SimpleNamespace(result=None))


def test_chain_cli_reports_a_task_removed_while_waiting(monkeypatch, capsys):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from govcon import cli
    from govcon.scheduler import chain_tasks
    from govcon.tasks import worker

    @contextmanager
    def session_scope(_):
        yield SimpleNamespace(get=lambda *_: None)

    monkeypatch.setattr(cli, "session_scope", session_scope)
    monkeypatch.setattr(chain_tasks, "queue_chain", lambda *_args, **_kwargs: (SimpleNamespace(id=42), True))
    monkeypatch.setattr(worker, "run_once", lambda *_args, **_kwargs: "succeeded")
    assert cli._run_chain_task("daily", SimpleNamespace()) is None
    assert "task 42 is no longer available" in capsys.readouterr().err
