"""Require zero Ruff and mypy diagnostics for release."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {"ruff": "0.16.10", "mypy": "2.4.0"}


def diagnostic_key(tool: str, path: str, code: str, message: str) -> str:
    normalized = path.replace("\\", "/")
    root = str(ROOT).replace("\\", "/") + "/"
    if normalized.casefold().startswith(root.casefold()):
        normalized = normalized[len(root):]
    return json.dumps([tool, normalized, code, message], ensure_ascii=True)


def collect() -> Counter[str]:
    diagnostics: Counter[str] = Counter()
    for tool, expected in VERSIONS.items():
        if version(tool) != expected:
            raise RuntimeError(f"{tool} must be {expected}; install the project's dev dependencies")
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(ROOT / "pyproject.toml"),
         "--output-format", "json", "src", "tests", "scripts/check_static.py", ".quality/stubs"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    if lint.returncode not in {0, 1} or lint.stderr.strip():
        raise RuntimeError(f"Ruff could not complete: {lint.stderr.strip()}")
    for row in json.loads(lint.stdout):
        diagnostics[diagnostic_key("ruff", row["filename"], row["code"], row["message"])] += 1
    types = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(ROOT / "pyproject.toml"),
         "--python-executable", sys.executable, "--no-incremental", "--no-color-output", "src/govcon"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )
    if types.returncode not in {0, 1} or types.stderr.strip():
        raise RuntimeError(f"Mypy could not complete: {types.stderr.strip()}")
    found = 0
    for line in types.stdout.splitlines():
        match = re.fullmatch(r"(.+?):\d+(?::\d+)?: error: (.+)  \[([^]]+)\]", line)
        if match:
            path, message, code = match.groups()
            diagnostics[diagnostic_key("mypy", path, code, message)] += 1
            found += 1
        elif ": error:" in line:
            raise RuntimeError(f"Unrecognized mypy diagnostic: {line}")
    if types.returncode == 1 and not found:
        raise RuntimeError("Mypy failed without reporting diagnostics")
    return diagnostics


def main() -> int:
    current = collect()
    for key, count in sorted(current.items()):
        tool, path, code, message = json.loads(key)
        print(f"{tool}: {path}: {code} {message} ({count})")
    for tool in VERSIONS:
        total = sum(count for key, count in current.items() if json.loads(key)[0] == tool)
        print(f"{tool}: {total} diagnostics")
    print("Static gate: FAIL" if current else "Static gate: PASS")
    return 1 if current else 0


if __name__ == "__main__":
    raise SystemExit(main())
