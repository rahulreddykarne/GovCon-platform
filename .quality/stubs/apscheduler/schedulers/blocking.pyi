from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.util import _Undefined

class BlockingScheduler(BaseScheduler):
    def __init__(self, *, timezone: str, jobstores: Mapping[str, SQLAlchemyJobStore], job_defaults: Mapping[str, object]) -> None: ...
    def add_job(self, func: Callable[..., Any], trigger: CronTrigger, *, id: str,
                replace_existing: bool, next_run_time: datetime | None | _Undefined,
                args: Sequence[object], name: str, misfire_grace_time: int, coalesce: bool) -> Job: ...
    def start(self) -> None: ...
