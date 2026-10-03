from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from apscheduler.schedulers import SchedulerNotRunningError as SchedulerNotRunningError
from apscheduler.triggers.base import BaseTrigger
from apscheduler.util import _Undefined

class BaseScheduler:
    def __init__(self, gconfig: Mapping[str, Any] = ..., **options: Any) -> None: ...
    def start(self, paused: bool = ...) -> None: ...
    def shutdown(self, wait: bool = ...) -> None: ...
    @property
    def running(self) -> bool: ...
    def add_job(
        self,
        func: Callable[..., Any] | str,
        trigger: BaseTrigger | str | None = ...,
        args: list[Any] | tuple[Any, ...] | None = ...,
        kwargs: dict[str, Any] | None = ...,
        id: str | None = ...,
        name: str | None = ...,
        misfire_grace_time: int | None | _Undefined = ...,
        coalesce: bool | _Undefined = ...,
        max_instances: int | _Undefined = ...,
        next_run_time: datetime | None | _Undefined = ...,
        jobstore: str = ...,
        executor: str = ...,
        replace_existing: bool = ...,
        **trigger_args: Any,
    ) -> Any: ...
