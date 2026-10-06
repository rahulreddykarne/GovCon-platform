from apscheduler.triggers.base import BaseTrigger

class CronTrigger(BaseTrigger):
    def __init__(
        self,
        year: int | str | None = ...,
        month: int | str | None = ...,
        day: int | str | None = ...,
        week: int | str | None = ...,
        day_of_week: int | str | None = ...,
        hour: int | str | None = ...,
        minute: int | str | None = ...,
        second: int | str | None = ...,
        start_date: object | None = ...,
        end_date: object | None = ...,
        timezone: str | None = ...,
        jitter: int | None = ...,
    ) -> None: ...
