from apscheduler.jobstores.base import BaseJobStore

class SQLAlchemyJobStore(BaseJobStore):
    def __init__(
        self,
        url: str | None = ...,
        engine: object | None = ...,
        tablename: str = ...,
        metadata: object | None = ...,
        pickle_protocol: int = ...,
        tableschema: str | None = ...,
        engine_options: dict[str, object] | None = ...,
    ) -> None: ...
