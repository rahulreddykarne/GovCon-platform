"""Instrumentation must identify failures without changing work or exposing content."""

from __future__ import annotations

import inspect
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from govcon.ai.replay import CallNeeded, active_recorder, run_recorded
from govcon.config import Settings
from govcon.diagnostics import diagnostic_event, pipeline_phase, trace_phase
from govcon.enrich.ocr import OcrConfig, ocr_pdf_pages
from govcon.logging import configure_logging


@pytest.fixture
def events():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, json.loads(record.getMessage())))

    logger = logging.getLogger("govcon.pipeline")
    old_level, old_disabled = logger.level, logger.disabled
    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    logger.disabled = old_disabled


def test_nested_phases_share_trace_and_restore_context(events):
    with pipeline_phase("outer", opportunity_id=17):
        with pipeline_phase("inner", task_id=23):
            diagnostic_event("detail", characters=40, payload="private body", password="secret")
        diagnostic_event("after_inner")
    with pipeline_phase("separate"):
        pass
    by_event = {item["event"]: item for _, item in events if item["event"] in {"detail", "after_inner"}}
    assert by_event["detail"]["opportunity_id"] == 17
    assert by_event["detail"]["task_id"] == 23
    assert "payload" not in by_event["detail"] and "password" not in by_event["detail"]
    assert by_event["after_inner"]["phase"] == "outer"
    assert "task_id" not in by_event["after_inner"]
    first, last = events[0][1], events[-1][1]
    assert first["trace_id"] != last["trace_id"]
    assert "opportunity_id" not in last


def test_exception_rethrown_with_safe_failure_locations(events):
    error = ValueError("sensitive document body and api_key=do-not-log")
    with pytest.raises(ValueError) as caught, pipeline_phase("failing", task_id=9):
        raise error
    assert caught.value is error
    failure = next(item for level, item in events if item["event"] == "phase.error")
    assert failure["error_type"] == "ValueError" and failure["task_id"] == 9
    locations = next(item for _, item in events if item["event"] == "phase.error_location")
    assert locations["frames"][-1]["function"] == "test_exception_rethrown_with_safe_failure_locations"
    assert locations["frames"][-1]["line"] > 0
    serialized = json.dumps(events)
    assert "sensitive document" not in serialized and "do-not-log" not in serialized
    assert not any(item["event"] == "phase.complete" for _, item in events)


def test_replay_control_flow_is_deferred_and_never_error(events):
    needed = CallNeeded(("synthetic", 1), 0, lambda: None)
    with pytest.raises(CallNeeded) as caught, pipeline_phase("provider"):
        raise needed
    assert caught.value is needed
    assert any(item["event"] == "phase.deferred" for _, item in events)
    assert all(level < logging.ERROR for level, _ in events)


def test_decorator_preserves_signature_result_and_identifiers(events):
    @trace_phase("test.function")
    def calculate(ctx, *, count=2):
        return {"status": "completed", "private_text": "never include this"}

    assert list(inspect.signature(calculate).parameters) == ["ctx", "count"]
    result = calculate(SimpleNamespace(task_id=21, opportunity_id=31))
    assert result["private_text"] == "never include this"
    assert all(item["task_id"] == 21 and item["opportunity_id"] == 31 for _, item in events)
    assert events[-2][1]["status"] == "completed"
    assert "never include this" not in json.dumps(events)


def test_thread_contexts_do_not_cross_contaminate(events):
    def run(identifier):
        with pipeline_phase("thread", opportunity_id=identifier):
            diagnostic_event("thread_detail")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [101, 202]))
    traces = {}
    for _, item in events:
        traces.setdefault(item["opportunity_id"], set()).add(item["trace_id"])
    assert len(traces[101]) == len(traces[202]) == 1
    assert traces[101].isdisjoint(traces[202])


def test_real_ocr_disabled_branch_explains_skipped_work(events):
    read, failed = ocr_pdf_pages(b"private document", [1, 2], OcrConfig(enabled=False))
    assert not read and set(failed) == {1, 2}
    unavailable = next(item for _, item in events if item["event"] == "ocr.unavailable")
    assert unavailable["reason"] == "disabled" and unavailable["pages"] == 2
    assert "private document" not in json.dumps(events)


def test_real_replay_reports_cache_and_checkpoints_without_extra_calls(events):
    performed = []
    checkpoints = []

    def run_pass():
        recorder = active_recorder()
        assert recorder is not None
        return recorder.call({"prompt": "private prompt"}, lambda: performed.append(1) or "private reply")

    assert run_recorded(run_pass, persist=checkpoints.append) == "private reply"
    assert performed == [1] and len(checkpoints) == 1
    lookups = [item["cached"] for _, item in events if item["event"] == "ai.replay_lookup"]
    assert lookups == [False, True]
    assert any(item["event"] == "ai.replay_checkpoint_saved" for _, item in events)
    assert "private prompt" not in json.dumps(events) and "private reply" not in json.dumps(events)


def test_structured_ai_logs_schema_field_and_successful_retry(events, monkeypatch):
    from pydantic import BaseModel

    from govcon.ai import structured
    from govcon.ai.providers.base import CompletionResult
    from govcon.security.classification import DataClassification

    class Output(BaseModel):
        count: int

    replies = iter([
        CompletionResult(content='{"count":"private-invalid-value"}', model="test-model", provider="fake", usage={}, latency_ms=2),
        CompletionResult(content='{"count":7}', model="test-model", provider="fake", usage={}, latency_ms=3),
    ])
    monkeypatch.setattr(structured, "complete_with_budget", lambda *args, **kwargs: (next(replies), None))
    prepared = SimpleNamespace(
        opportunity_id=61, provider=object(), provider_name="fake", model="test-model",
        prompt=SimpleNamespace(name="synthetic"), schema_cls=Output, schema_version="test.v1",
        system_prompt="private system prompt", user_prompt="private user prompt", classification=DataClassification.PUBLIC,
    )
    executed = structured.execute_prepared_call(prepared, settings=Settings(prompt_max_retries_on_invalid_json=1))
    assert executed.output.count == 7
    field = next(item for _, item in events if item["event"] == "ai.schema_field_rejected")
    assert field["stage"] == "count" and field["reason"] == "int_parsing"
    attempts = [item["attempt"] for _, item in events if item["event"] == "ai.validation_attempt"]
    assert attempts == [1, 2]
    assert any(item["event"] == "ai.output_validated" for _, item in events)
    serialized = json.dumps(events)
    assert "private-invalid-value" not in serialized
    assert "private system prompt" not in serialized and "private user prompt" not in serialized


def test_provider_retry_reports_status_without_leaking_request(events, monkeypatch):
    from govcon.ai import budget
    from govcon.ai.providers.base import CompletionResult, ProviderAPIError

    attempts = []

    class Provider:
        name = "fake"

        def complete(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderAPIError("fake", 503)
            return CompletionResult(content="private reply", model="test-model", provider="fake", usage={}, latency_ms=3)

    monkeypatch.setattr(budget.time, "sleep", lambda seconds: None)
    result, reservation = budget._call_provider(
        Provider(), None, opportunity_id=None, settings=Settings(ai_max_provider_retries=1),
        engine=None, requested_model="test-model",
        kwargs={"system_prompt": "private system", "user_prompt": "private user", "purpose": "synthetic"},
    )
    assert result.content == "private reply" and reservation is None and len(attempts) == 2
    failed = next(item for _, item in events if item["event"] == "ai.provider_failed")
    assert failed["http_status"] == 503 and failed["attempt"] == 1
    assert any(item["event"] == "ai.provider_retry" for _, item in events)
    assert "private reply" not in json.dumps(events) and "private user" not in json.dumps(events)


def test_log_level_rotation_and_redaction(tmp_path, monkeypatch):
    logger = logging.getLogger("govcon")
    saved_handlers = logger.handlers[:]
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.handlers = []
    try:
        settings = Settings(log_dir=tmp_path, log_level="DEBUG", log_max_bytes=500, log_backup_count=2)
        configure_logging(settings, force=True)
        logger.debug("debug-visible password=synthetic-secret")
        contents = (tmp_path / "govcon.log").read_text(encoding="utf-8")
        assert "debug-visible" in contents and "synthetic-secret" not in contents
        for index in range(30):
            logger.debug("rotation %s %s", index, "x" * 90)
        paths = list(tmp_path.glob("govcon.log*"))
        assert len(paths) == 3
        assert all(path.stat().st_size < 500 for path in paths)
        settings.log_level = "INFO"
        configure_logging(settings)
        logger.debug("debug-hidden")
        logger.info("info-visible")
        contents = (tmp_path / "govcon.log").read_text(encoding="utf-8")
        assert "debug-hidden" not in contents and "info-visible" in contents
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


@pytest.mark.parametrize("failure_stage", [None, "prepare", "execute", "publish"])
def test_worker_reports_exact_failing_stage_and_preserves_outcome(events, monkeypatch, failure_stage):
    from govcon.tasks import worker
    from govcon.tasks.queue import Claim
    from govcon.tasks.registry import Step, TaskHandler

    calls = []
    task = SimpleNamespace(id=41, opportunity_id=51, status="running", checkpoint={}, payload={}, input_revision={})

    class Heartbeat:
        lost = False

        def __init__(self, *args):
            pass

        def start(self):
            pass

        def stop(self):
            calls.append("heartbeat_stopped")

        def set_deadline(self, seconds):
            pass

    def prepare(db, row, ctx):
        calls.append("prepare")
        if failure_stage == "prepare":
            raise RuntimeError("private prepare input")
        return "private input"

    def execute(inputs, ctx):
        calls.append("execute")
        if failure_stage == "execute":
            raise RuntimeError("private execute input")
        return "private output"

    def publish(db, row, output, ctx):
        calls.append("publish")
        if failure_stage == "publish":
            raise RuntimeError("private publish output")

    handler = TaskHandler("synthetic", steps=[Step("sample", prepare, publish, execute)])
    monkeypatch.setattr(worker, "get_handler", lambda name: handler)
    monkeypatch.setattr(worker, "_Heartbeat", Heartbeat)
    monkeypatch.setattr(worker, "session_scope", lambda settings: nullcontext(None))
    monkeypatch.setattr(worker.queue, "guard_publish", lambda db, claim: task)
    monkeypatch.setattr(worker.queue, "record_step", lambda row, name, data: setattr(row, "checkpoint", {"completed_steps": [name]}))
    monkeypatch.setattr(worker.queue, "complete", lambda row, result: setattr(row, "status", "completed"))
    monkeypatch.setattr(worker, "_record_outcome", lambda settings, claim, exc: "failed")
    claim = Claim(41, "synthetic", 51, 1, "test-worker")
    assert worker.run_claimed(Settings(), claim) == ("failed" if failure_stage else "completed")
    assert calls[-1] == "heartbeat_stopped"
    errors = [item for _, item in events if item["event"] == "phase.error"]
    if failure_stage:
        assert len(errors) == 1
        assert errors[0]["phase"] == f"worker.{failure_stage}"
        assert errors[0]["step"] == "sample"
        assert errors[0]["task_id"] == 41 and errors[0]["opportunity_id"] == 51
        assert "publish" not in calls or failure_stage == "publish"
    else:
        assert not errors
        assert any(item["event"] == "task.checkpoint_saved" for _, item in events)
    assert "private input" not in json.dumps(events) and "private output" not in json.dumps(events)
