# Static release checks

Install the pinned development dependencies with `python -m pip install -e ".[dev]"`,
then run `python scripts/check_static.py`. CI runs this gate on Linux and Windows.
Every Ruff or mypy diagnostic fails the gate. There are no baseline allowances.
Tool failures, unrecognized diagnostic output, and version mismatches also fail.

Ruff checks syntax, Pyflakes errors, and import ordering (`E9`, `F`, `I`) in
`src`, `tests`, the gate script, and `.quality/stubs`. Mypy checks all modules in
`src/govcon` against Python 3.12 and the interpreter's installed dependencies,
with incremental caching disabled in the release gate. Configuration lives in
`pyproject.toml`; user-wide settings do not define the release checks.

The development dependencies include published stubs for setuptools, openpyxl,
and reportlab. `.quality/stubs` describes the concrete APScheduler, pypdfium2,
and pytesseract interfaces used by this project; these dependencies do not ship
fully usable typing information. These local interfaces were checked against
installed library source. They are development inputs, not runtime replacements.
Extend them against the real library API when adding new calls; do not suppress
missing interfaces with blanket import ignores or permissive attribute fallbacks.

Raw checks can also be run directly:

```sh
python -m ruff check src tests scripts/check_static.py .quality/stubs
python -m mypy --no-incremental src/govcon
```

The cleanup removed the former 454 Ruff and 369 mypy diagnostics. Keep checks
at zero and verify behavioral changes with relevant regression tests. Do not
restore a baseline or weaken checking rules to accommodate new errors.
