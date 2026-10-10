# Mypy stubs

`pypdfium2` and `pytesseract` ship without a `py.typed` marker or typeshed stubs.
The `.pyi` files here declare only the attributes `src/govcon/enrich/ocr.py` uses.

`apscheduler` likewise has no published stubs. `stubs/apscheduler` declares the
job store, scheduler, and cron trigger symbols `src/govcon/scheduler/runner.py` uses.

`pyproject.toml` sets `mypy_path = "stubs"` so mypy finds them. That path is for
the type checker only; do not add `stubs/` to `PYTHONPATH`, or these modules
would shadow the installed packages at runtime.

From the repository root:

```
/tmp/govcon-venv/bin/mypy --python-version 3.12 src/govcon/enrich src/govcon/ai src/govcon/proposals src/govcon/prompting src/govcon/decision
```

The same command sees the stubs when `MYPYPATH=stubs` is set instead of `mypy_path`.
