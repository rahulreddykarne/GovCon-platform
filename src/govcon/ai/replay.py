"""Record and replay AI calls so a service never holds a transaction during one (DEV-023).

The solicitation summary, the compliance pipeline and the decision engine
interleave database reads and writes with provider calls in one session.
:func:`run_recorded` runs such a service in passes:

1. each pass opens a short transaction, takes its locks in the workflow order
   (opportunity first) and runs the service from the start;
2. a provider call whose response is already recorded is answered from the
   record, with no network call;
3. the first call with no recorded response stops the pass (``CallNeeded``).
   The pass rolls back, the call runs with no transaction open, and its
   response, or its error, is recorded;
4. a pass that reaches the end with every call answered commits.

Responses are matched on the whole request and on its occurrence, so an
identical request repeated by a retry (for example after invalid JSON) is a
new call, as it is in a live run. A recorded error is raised again where the
service asked for it, so budget exhaustion and provider failures take the
same path they take live.

Passes must build the same requests. Two things would otherwise differ:

- **Row ids.** Sequences do not roll back, so a row inserted again in a later
  pass would get a new id, and prompts that cite requirement or evidence ids
  would change. :func:`stable_ids` gives the n-th new row of each table the id
  it got in the earlier pass. Those ids came from the sequence and were rolled
  back, so nothing else can hold them.
- **The clock.** Callers pass one ``now`` to every pass.

If a pass still asks for a different request where an earlier pass asked for
a recorded one, the inputs changed between passes. The new request is called.
After ``max_divergences`` such changes the remaining calls run inside the
pass, as they did before recording existed, so the step still finishes.
A pass that loses a deadlock or serialization conflict runs again with the
responses already recorded.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import Integer, event, inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

logger = logging.getLogger("govcon.ai.replay")

_active: ContextVar[Recorder | None] = ContextVar("govcon_ai_recorder", default=None)
# Deadlock detected, serialization failure.
_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
_MAX_PASS_CONFLICTS = 3


class CallNeeded(BaseException):
    """Stops a recorded pass at a call with no recorded response.

    A ``BaseException``, so a service's own ``except Exception`` handlers,
    which turn provider failures into warnings, cannot swallow it.
    """

    def __init__(self, key: tuple[str, int], position: int, perform: Callable[[], Any]) -> None:
        super().__init__("AI call needed outside the transaction")
        self.key = key
        self.position = position
        self.perform = perform


@dataclass
class _Outcome:
    value: Any = None
    error: BaseException | None = None


class Recorder:
    def __init__(self, *, max_divergences: int = 3, max_calls: int = 500) -> None:
        self.max_divergences = max_divergences
        self.max_calls = max_calls
        self.divergences = 0
        self.calls_made = 0
        self.passes = 0
        # Set when the limits are reached: remaining calls run inside the pass.
        self.live = False
        self._outcomes: dict[tuple[str, int], _Outcome] = {}
        self._order: list[tuple[str, int]] = []
        self._seen: Counter[str] = Counter()
        self._position = 0
        self._ids: dict[str, list[int]] = defaultdict(list)
        self._ids_used: Counter[str] = Counter()

    def start_pass(self) -> None:
        self.passes += 1
        self._seen = Counter()
        self._position = 0
        self._ids_used = Counter()

    def call[T](self, request: Any, perform: Callable[[], T]) -> T:
        """Answer ``request`` from the record, or stop the pass to make the call."""
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._seen[digest] += 1
        key = (digest, self._seen[digest])
        position = self._position
        self._position += 1
        outcome = self._outcomes.get(key)
        if outcome is None:
            needed = CallNeeded(key, position, perform)
            if not self.live:
                raise needed
            self._perform(needed)
            outcome = self._outcomes[key]
        if outcome.error is not None:
            raise outcome.error
        return outcome.value

    def perform(self, needed: CallNeeded) -> None:
        """Make the call with no transaction open and record its response or error."""
        if needed.position < len(self._order):
            self.divergences += 1
            del self._order[needed.position:]
        if self.divergences > self.max_divergences or self.calls_made >= self.max_calls:
            # Do not loop forever on inputs that keep changing: finish in one pass.
            self.live = True
            logger.warning("AI inputs changed %d times between passes (%d calls); finishing the step in one "
                           "transaction", self.divergences, self.calls_made)
            return
        self._perform(needed)

    def _perform(self, needed: CallNeeded) -> None:
        self.calls_made += 1
        try:
            outcome = _Outcome(value=needed.perform())
        except Exception as exc:  # noqa: BLE001 - recorded and raised again where the service asked
            outcome = _Outcome(error=exc)
        self._outcomes[needed.key] = outcome
        self._order.append(needed.key)

    # Row ids --------------------------------------------------------------

    def assign_id(self, table: str) -> int | None:
        """The id the next new row of ``table`` got in an earlier pass, if any."""
        used = self._ids_used[table]
        if used < len(self._ids[table]):
            self._ids_used[table] += 1
            return self._ids[table][used]
        return None

    def record_id(self, table: str, value: int) -> None:
        self._ids[table].append(value)
        self._ids_used[table] += 1

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe copy of the record, so a crashed worker can resume it."""
        outcomes: list[dict[str, Any]] = []
        for digest, occurrence in self._order:
            outcome = self._outcomes[(digest, occurrence)]
            outcomes.append({
                "digest": digest,
                "occurrence": occurrence,
                "value": None if outcome.error is not None else _freeze(outcome.value),
                "error": _freeze_error(outcome.error) if outcome.error is not None else None,
            })
        return {
            "max_divergences": self.max_divergences,
            "max_calls": self.max_calls,
            "divergences": self.divergences,
            "calls_made": self.calls_made,
            "live": self.live,
            "outcomes": outcomes,
            "ids": {table: list(values) for table, values in self._ids.items()},
        }

    @classmethod
    def restore(cls, payload: dict[str, Any] | None, *, max_divergences: int = 3, max_calls: int = 500) -> Recorder:
        recorder = cls(
            max_divergences=int((payload or {}).get("max_divergences", max_divergences)),
            max_calls=int((payload or {}).get("max_calls", max_calls)),
        )
        if not payload:
            return recorder
        recorder.divergences = int(payload.get("divergences") or 0)
        recorder.calls_made = int(payload.get("calls_made") or 0)
        recorder.live = bool(payload.get("live"))
        for table, values in (payload.get("ids") or {}).items():
            recorder._ids[str(table)] = [int(value) for value in values]
        for item in payload.get("outcomes") or []:
            key = (str(item["digest"]), int(item["occurrence"]))
            error = _thaw_error(item.get("error")) if item.get("error") else None
            recorder._outcomes[key] = _Outcome(value=None if error else _thaw(item.get("value")), error=error)
            recorder._order.append(key)
        return recorder


def active_recorder() -> Recorder | None:
    return _active.get()


def _integer_identity(obj: Any) -> tuple[str, str] | None:
    """``(table, attribute)`` for a row whose primary key is one generated integer."""
    mapper = inspect(obj).mapper
    if len(mapper.primary_key) != 1:
        return None
    column = mapper.primary_key[0]
    if not isinstance(column.type, Integer) or column.autoincrement is False:
        return None
    return column.table.name, mapper.get_property_by_column(column).key


@contextmanager
def stable_ids(session: Session) -> Iterator[None]:
    """Give new rows in ``session`` the ids they got in the earlier passes."""
    recorder = _active.get()
    if recorder is None:
        yield
        return
    pending: list[tuple[str, Any, str]] = []

    def before_flush(flush_session, flush_context, instances) -> None:
        for obj in list(flush_session.new):
            identity = _integer_identity(obj)
            if identity is None or getattr(obj, identity[1]) is not None:
                continue
            table, attribute = identity
            known = recorder.assign_id(table)
            if known is None:
                pending.append((table, obj, attribute))
            else:
                setattr(obj, attribute, known)

    def after_flush(flush_session, flush_context) -> None:
        for table, obj, attribute in pending:
            value = getattr(obj, attribute)
            if value is not None:
                recorder.record_id(table, value)
        pending.clear()

    event.listen(session, "before_flush", before_flush)
    event.listen(session, "after_flush", after_flush)
    try:
        yield
    finally:
        event.remove(session, "before_flush", before_flush)
        event.remove(session, "after_flush", after_flush)


def _retryable_conflict(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) in _RETRYABLE_SQLSTATES


def run_recorded[T](
    run_pass: Callable[[], T],
    *,
    max_divergences: int = 3,
    max_calls: int = 500,
    restore: dict[str, Any] | None = None,
    persist: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    """Run ``run_pass`` until a pass completes with every AI call answered.

    ``run_pass`` must open and close its own transaction, so a stopped pass
    rolls back before the call is made, and should open it under
    :func:`stable_ids`. ``persist`` is called after each out-of-transaction
    call so the record survives a worker crash. ``restore`` is that record.
    """
    if _active.get() is not None:
        raise RuntimeError("recorded runs cannot be nested")
    recorder = Recorder.restore(restore, max_divergences=max_divergences, max_calls=max_calls)
    conflicts = 0
    while True:
        token = _active.set(recorder)
        recorder.start_pass()
        pending: CallNeeded | None = None
        try:
            return run_pass()
        except CallNeeded as needed:
            pending = needed
        except DBAPIError as exc:
            conflicts += 1
            if not _retryable_conflict(exc) or conflicts > _MAX_PASS_CONFLICTS:
                raise
            logger.warning("recorded pass lost a database conflict (%s); running it again",
                           getattr(exc.orig, "sqlstate", None))
        finally:
            _active.reset(token)
        if pending is not None:
            recorder.perform(pending)
            if persist is not None:
                persist(recorder.snapshot())


def _freeze(value: Any) -> Any:
    from govcon.ai.budget import Reservation
    from govcon.ai.providers.base import CompletionResult

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return {"__replay__": "tuple", "items": [_freeze(item) for item in value]}
    if isinstance(value, list):
        return {"__replay__": "list", "items": [_freeze(item) for item in value]}
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, CompletionResult):
        return {
            "__replay__": "completion",
            "content": value.content,
            "model": value.model,
            "provider": value.provider,
            "usage": dict(value.usage or {}),
            "latency_ms": value.latency_ms,
            "finish_reason": value.finish_reason,
        }
    if isinstance(value, Reservation):
        return {"__replay__": "reservation"}
    if isinstance(value, httpx.Response):
        return {
            "__replay__": "http_response",
            "status_code": value.status_code,
            "body": value.content.decode("utf-8", "replace"),
        }
    raise TypeError(f"AI replay cannot store a {type(value).__name__}")


def _thaw(value: Any) -> Any:
    from govcon.ai.providers.base import CompletionResult

    if not isinstance(value, dict) or "__replay__" not in value:
        if isinstance(value, dict):
            return {key: _thaw(item) for key, item in value.items()}
        return value
    kind = value["__replay__"]
    if kind == "tuple":
        return tuple(_thaw(item) for item in value["items"])
    if kind == "list":
        return [_thaw(item) for item in value["items"]]
    if kind == "completion":
        return CompletionResult(
            content=value["content"], model=value["model"], provider=value["provider"],
            usage=dict(value.get("usage") or {}), latency_ms=int(value.get("latency_ms") or 0),
            finish_reason=value.get("finish_reason"),
        )
    if kind == "reservation":
        return _RestoredReservation()
    if kind == "http_response":
        body = value.get("body") or ""
        return httpx.Response(int(value["status_code"]), content=body.encode("utf-8"))
    raise TypeError(f"AI replay cannot restore {kind}")


class _RestoredReservation:
    """A budget reservation that already finished before the worker crashed."""

    def finish(self, result: Any = None) -> None:
        return None


def _freeze_error(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "__replay__": "error",
        "qualname": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc)[:500],
    }
    for name in ("provider", "status_code", "model", "category"):
        if hasattr(exc, name):
            payload[name] = getattr(exc, name)
    return payload


def _thaw_error(payload: dict[str, Any]) -> BaseException:
    qualname = str(payload.get("qualname") or "RuntimeError")
    module_name, _, class_name = qualname.rpartition(".")
    cls: type[BaseException] | None = None
    if module_name:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError):
            cls = None
    message = str(payload.get("message") or "recorded provider error")
    if cls is not None and issubclass(cls, BaseException):
        try:
            if class_name == "ProviderAPIError":
                return cls(str(payload.get("provider") or "provider"), payload.get("status_code"))
            return cls(message)
        except Exception:  # noqa: BLE001  boundary must record any failure
            logger.warning("could not restore recorded error %s", qualname)
    return RuntimeError(message)
