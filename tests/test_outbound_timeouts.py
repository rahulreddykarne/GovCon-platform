"""Connect timeouts, IPv4-first dialing, worker heartbeat, and interrupted bot runs."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpcore
import httpx
from sqlalchemy.orm import Session

from govcon.bots.orchestrator import execute_orchestrator
from govcon.bots.store import (
    INTERRUPTED_REASON,
    begin_run,
    fail_interrupted_runs,
    owner_still_running,
    reset_bot_worker,
    set_bot_worker,
)
from govcon.config import Settings, get_settings
from govcon.http import (
    CONNECT_TIMEOUT_SECONDS,
    IPv4FirstBackend,
    build_client,
    connect_targets,
    split_timeout,
)
from govcon.models import BotRun
from govcon.tasks.worker import heartbeat_loop


def test_split_timeout_caps_connect_and_keeps_the_read_budget() -> None:
    timeout = split_timeout(90)
    assert timeout.connect == CONNECT_TIMEOUT_SECONDS
    assert timeout.read == 90
    client = build_client(Settings(http_user_agent="govcon-test"), timeout=90, transport=httpx.MockTransport(
        lambda request: httpx.Response(200),
    ))
    assert isinstance(client.timeout, httpx.Timeout)
    assert client.timeout.connect == CONNECT_TIMEOUT_SECONDS
    assert client.timeout.read == 90
    client.close()


def test_connect_targets_put_ipv4_first(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f28:40:5101::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("18.0.0.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f28:40:5102::1", 443, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f28:40:5103::1", 443, 0, 0)),
        ],
    )
    assert connect_targets("api.sam.gov", 443) == [
        "18.0.0.1",
        "2600:1f28:40:5101::1",
        "2600:1f28:40:5102::1",
    ]


def test_backend_tries_ipv4_before_ipv6_and_caps_connect(monkeypatch) -> None:
    monkeypatch.setattr(
        "govcon.http.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f28:40:5100::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("18.0.0.1", 443)),
        ],
    )
    tried: list[tuple[str, float | None]] = []

    def fake(self, host, port, timeout=None, local_address=None, socket_options=None):
        del self, port, local_address, socket_options
        tried.append((host, timeout))
        if ":" in host:
            raise OSError("ipv6 blackhole")
        return "stream"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake)
    stream = IPv4FirstBackend().connect_tcp("api.sam.gov", 443, timeout=90)
    assert stream == "stream"
    assert tried == [("18.0.0.1", CONNECT_TIMEOUT_SECONDS)]


def test_backend_falls_back_to_ipv6_after_ipv4_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "govcon.http.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("18.0.0.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2600:1f28:40:5100::1", 443, 0, 0)),
        ],
    )
    tried: list[str] = []

    def fake(self, host, port, timeout=None, local_address=None, socket_options=None):
        del self, port, timeout, local_address, socket_options
        tried.append(host)
        if host == "18.0.0.1":
            raise OSError("refused")
        return "v6"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake)
    assert IPv4FirstBackend().connect_tcp("api.sam.gov", 443, timeout=None) == "v6"
    assert tried == ["18.0.0.1", "2600:1f28:40:5100::1"]


def test_worker_heartbeat_keeps_firing_while_a_job_would_block() -> None:
    beats: list[str] = []

    def record(settings, role, instance_id) -> None:
        del settings
        assert role == "worker"
        beats.append(instance_id)

    from govcon.tasks import worker as worker_module

    original = worker_module._beat
    worker_module._beat = record
    stop = threading.Event()
    thread = threading.Thread(
        target=heartbeat_loop, args=(None, "worker-1", stop), kwargs={"interval": 0.02}, daemon=True,
    )
    try:
        thread.start()
        deadline = time.monotonic() + 1
        while len(beats) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(1)
        worker_module._beat = original
    assert beats[:2] == ["worker-1", "worker-1"]


def test_dead_worker_pid_is_not_treated_as_alive() -> None:
    host = socket.gethostname()
    worker_id = f"{host}:2147483647:dead"
    fresh = {worker_id: datetime.now(UTC)}
    assert owner_still_running(worker_id, fresh) is False
    assert owner_still_running("other-host:1:abc", {"other-host:1:abc": datetime.now(UTC)}) is True
    assert owner_still_running(None, {}) is False


def test_interrupted_bot_run_is_failed_and_can_start_again(upgraded_engine) -> None:
    key = f"orchestrator:timeout-{uuid4().hex}:pull=0"
    with Session(upgraded_engine) as session:
        dead = BotRun(
            bot_name="orchestrator",
            status="running",
            trigger="worker",
            idempotency_key=key,
            started_at=datetime.now(UTC),
            attempt=1,
            inputs={"slot": "timeout"},
            outputs={"worker_id": "gone:2147483647:abcd"},
        )
        live = BotRun(
            bot_name="discovery",
            status="running",
            trigger="worker",
            idempotency_key=f"discovery:timeout-{uuid4().hex}",
            started_at=datetime.now(UTC),
            attempt=1,
            inputs={},
            outputs={"worker_id": "still-here"},
        )
        session.add_all([dead, live])
        session.flush()
        changed = fail_interrupted_runs(session, owner_alive=lambda owner: owner == "still-here")
        assert changed >= 1
        assert dead.status == "failed"
        assert dead.error == INTERRUPTED_REASON
        assert dead.outputs["retryable"] is True
        assert dead.outputs["state"] == "incomplete"
        assert live.status == "running"
        again, started = begin_run(
            session, bot_name="orchestrator", idempotency_key=key, trigger="worker", inputs={"slot": "timeout"},
        )
        assert started is True
        assert again.status == "running"
        session.rollback()


def test_worker_start_is_stored_on_the_run(upgraded_engine) -> None:
    token = set_bot_worker("laptop:42:abc")
    try:
        with Session(upgraded_engine) as session:
            run, started = begin_run(
                session, bot_name="alert", idempotency_key=f"alert:owner-{uuid4().hex}",
                trigger="worker", inputs={},
            )
            assert started is True
            assert run.outputs["worker_id"] == "laptop:42:abc"
            session.rollback()
    finally:
        reset_bot_worker(token)


def test_persist_start_leaves_a_running_row_when_the_finish_is_lost(upgraded_engine, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("worker stopped during SAM")

    monkeypatch.setattr("govcon.bots.orchestrator.execute_discovery", boom)
    slot = f"hang-{uuid4().hex}"
    with Session(upgraded_engine) as session:
        run = execute_orchestrator(
            session, get_settings(), trigger="worker", slot=slot, pull=False, persist_start=True,
        )
        run_id = run.id
        assert run.status == "failed"  # in memory, after the caught error
        session.rollback()
    with Session(upgraded_engine) as session:
        stored = session.get(BotRun, run_id)
        assert stored is not None
        assert stored.status == "running"
        session.delete(stored)
        session.commit()
