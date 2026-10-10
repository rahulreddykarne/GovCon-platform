"""Audit round 2, finding 2: a transient database error must not stop a long-running worker.

``govcon worker start`` and the scheduler's embedded chain worker both run
``run_worker`` in its long-running mode. ``run_once`` is replaced here by a
script of outcomes so no database is involved.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from govcon.config import get_settings
from govcon.tasks import worker


@pytest.fixture(autouse=True)
def isolate_worker_startup(monkeypatch):
    """The scripted loop tests must not contact a real database at startup."""
    @contextmanager
    def session_scope(*args, **kwargs):
        yield Mock()

    monkeypatch.setattr(worker, "session_scope", session_scope)
    monkeypatch.setattr(worker, "fail_interrupted_runs", Mock(return_value=0))
    monkeypatch.setattr(worker, "_beat", Mock())
    monkeypatch.setattr("govcon.prompting.registry.ensure_prompt_registry", Mock(return_value=[]))


def _db_down() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


@pytest.fixture()
def settings():
    return get_settings().model_copy(update={"worker_poll_seconds": 0.01})


def _scripted(monkeypatch, outcomes, stop: threading.Event | None = None):
    """``run_once`` returns (or raises) each outcome in turn, then sets ``stop`` and idles."""
    calls = []

    def fake_run_once(settings, *, worker_id=None, task_types=None, **_):
        calls.append(task_types)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if stop is not None:
            stop.set()
        return None

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    return calls


# ── current behaviour that must not change ───────────────────────────────────

def test_until_idle_processes_due_tasks_then_returns(monkeypatch, settings):
    _scripted(monkeypatch, [(1, "succeeded"), (2, "failed")])
    assert worker.run_worker(settings, until_idle=True) == 2


def test_max_tasks_stops_the_worker(monkeypatch, settings):
    _scripted(monkeypatch, [(1, "succeeded"), (2, "succeeded"), (3, "succeeded")])
    assert worker.run_worker(settings, until_idle=True, max_tasks=2) == 2


def test_stop_event_stops_a_long_running_worker(monkeypatch, settings):
    stop = threading.Event()
    calls = _scripted(monkeypatch, [(1, "succeeded")], stop)
    assert worker.run_worker(settings, task_types=["scheduler_chain"], stop_event=stop) == 1
    assert calls[0] == ["scheduler_chain"]


def test_until_idle_run_still_reports_a_database_error(monkeypatch, settings):
    """``govcon worker run`` is a one-shot; it keeps surfacing the error to the caller."""
    _scripted(monkeypatch, [_db_down()])
    with pytest.raises(OperationalError):
        worker.run_worker(settings, until_idle=True)


# ── the finding ──────────────────────────────────────────────────────────────

def test_long_running_worker_survives_a_transient_database_error(monkeypatch, settings, caplog):
    stop = threading.Event()
    _scripted(monkeypatch, [_db_down(), (7, "succeeded"), _db_down(), (8, "succeeded")], stop)
    with caplog.at_level("ERROR", logger="govcon.tasks.worker"):
        processed = worker.run_worker(settings, stop_event=stop)
    assert processed == 2, "the worker kept running after the database came back"
    assert sum("OperationalError" in (r.exc_text or "") for r in caplog.records) == 2


def test_scheduler_embedded_chain_worker_survives_a_database_error(monkeypatch, settings):
    from govcon.scheduler import runner

    resumed = threading.Event()
    outcomes = [_db_down(), _db_down()]

    def fake_run_once(settings, *, worker_id=None, task_types=None, **_):
        if outcomes:
            raise outcomes.pop(0)
        resumed.set()

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    stop = runner._start_embedded_worker(settings)
    try:
        assert resumed.wait(5), "the chain worker thread died on the database error"
        assert any(t.name == "scheduler-chain-worker" and t.is_alive() for t in threading.enumerate())
    finally:
        stop.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(t.name == "scheduler-chain-worker" for t in threading.enumerate()):
        time.sleep(0.02)
