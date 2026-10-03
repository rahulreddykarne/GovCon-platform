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
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Integer, event, inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

logger = logging.getLogger("govcon.ai.replay")

T = TypeVar("T")

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
    error: Exception | None = None


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

    def call(self, request: Any, perform: Callable[[], T]) -> T:
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


def run_recorded(  # noqa: UP047 - TypeVar keeps mypy's older target parsing this module
    run_pass: Callable[[], T], *, max_divergences: int = 3, max_calls: int = 500
) -> T:
    """Run ``run_pass`` until a pass completes with every AI call answered.

    ``run_pass`` must open and close its own transaction, so a stopped pass
    rolls back before the call is made, and should open it under
    :func:`stable_ids`.
    """
    if _active.get() is not None:
        raise RuntimeError("recorded runs cannot be nested")
    recorder = Recorder(max_divergences=max_divergences, max_calls=max_calls)
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
