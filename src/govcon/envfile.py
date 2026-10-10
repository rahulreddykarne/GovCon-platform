"""Read a local ``.env`` without printing values.

Process environment wins over the file. A repeated key in the file is one
entry; the last value is kept, matching python-dotenv (and therefore the
settings object). Blank and whitespace-only values count as absent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.parse import urlencode

PRESENCE_NAMES = (
    "SAM_API_KEY",
    "DEEPSEEK_API_KEY",
    "JEV_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SMTP_HOST",
)

_ARRAY_METADATA = frozenset({
    "Count",
    "Length",
    "LongLength",
    "Rank",
    "SyncRoot",
    "IsReadOnly",
    "IsFixedSize",
    "IsSynchronized",
})

_HEALTH_UNAVAILABLE = (
    "GET /health  unavailable. Start the web process with Start-GovCon.ps1. "
    "The error text is omitted because it can contain a URL."
)


def parse_env_text(text: str) -> dict[str, str]:
    """Parse dotenv text. Quotes, a leading BOM, CRLF, and inline comments are handled."""
    from dotenv import dotenv_values

    cleaned = text.lstrip("\ufeff")
    parsed = dotenv_values(stream=StringIO(cleaned))
    values: dict[str, str] = {}
    for key, raw in parsed.items():
        if not key:
            continue
        name = key.replace("\ufeff", "").strip()
        if not name:
            continue
        values[name] = "" if raw is None else raw.strip()
    return values


def load_env_file(path: str | Path | None) -> dict[str, str]:
    """Return parsed values. A missing path is an empty mapping, not an error."""
    if path is None or str(path) == "":
        return {}
    file = Path(path)
    if not file.is_file():
        return {}
    return parse_env_text(file.read_text(encoding="utf-8-sig"))


def lookup(mapping: Mapping[str, str], name: str) -> str | None:
    """Return a defined value, or None when the name is absent. Blank stays blank."""
    for key, value in mapping.items():
        if key.upper() == name.upper():
            return "" if value is None else str(value).strip()
    return None


def resolved_value(name: str, process: Mapping[str, str], file_values: Mapping[str, str]) -> str:
    """Value to use for a call. Empty when the name is absent. Do not print this."""
    process_value = lookup(process, name)
    if process_value is not None:
        return process_value
    return lookup(file_values, name) or ""


def credential_state(name: str, process: Mapping[str, str], file_values: Mapping[str, str]) -> str:
    return "present" if resolved_value(name, process, file_values) else "absent"


def presence_lines(
    names: tuple[str, ...] | list[str],
    process: Mapping[str, str],
    file_values: Mapping[str, str],
) -> list[str]:
    return [f"{name}  {credential_state(name, process, file_values)}" for name in names]


def fill_process_from_file(process: MutableMapping[str, str], file_values: Mapping[str, str]) -> None:
    """Copy file values only for names the process has not defined."""
    for key, value in file_values.items():
        if lookup(process, key) is None and value.strip():
            process[key] = value.strip()


def format_health_report(http_status: str, body: str) -> list[str]:
    """One line for the response, then one line per check. Never print array metadata."""
    if http_status == "unavailable":
        return [_HEALTH_UNAVAILABLE]
    try:
        payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        return [f"GET /health  {http_status}  unreadable. The body was not printed."]
    if not isinstance(payload, dict):
        return [f"GET /health  {http_status}  unreadable. The body was not printed."]
    status = payload.get("status") or ""
    lines = [f"GET /health  {http_status}  status={status}"]
    lines.extend(_check_lines(payload.get("checks")))
    return lines


def _check_lines(checks: Any) -> list[str]:
    if isinstance(checks, list):
        return [_one_check(item) for item in checks]
    if isinstance(checks, dict):
        lines: list[str] = []
        for name, value in checks.items():
            if name in _ARRAY_METADATA and not isinstance(value, dict):
                continue
            if isinstance(value, dict):
                lines.append(_one_check({"name": name, **value}))
            else:
                lines.append(f"  {name}  {value}")
        return lines
    return []


def _one_check(item: Any) -> str:
    if not isinstance(item, dict):
        return f"  {item}"
    name = str(item.get("name") or "").replace("\n", " ").strip()
    status = str(item.get("status") or "").replace("\n", " ").strip()
    detail = str(item.get("detail") or "").replace("\r", " ").replace("\n", " ").strip()
    return f"  {name}  {status}  {detail}".rstrip()


# A live SAM search of a short window took about 66 seconds.
SAM_PROBE_TIMEOUT = 90.0
DEEPSEEK_PROBE_TIMEOUT = 30.0


def sam_probe_url(api_key: str, *, today: date | None = None) -> str:
    """Opportunities search for yesterday through today, one row. The key is in the query."""
    from govcon.ingest.sam_opportunities import SAM_SEARCH_URL, format_sam_date

    end = today or datetime.now(UTC).date()
    start = end - timedelta(days=1)
    query = urlencode({
        "api_key": api_key,
        "postedFrom": format_sam_date(start),
        "postedTo": format_sam_date(end),
        "limit": "1",
    })
    return f"{SAM_SEARCH_URL}?{query}"


def live_lines(
    process: Mapping[str, str],
    fetch: Callable[[str, str, dict[str, str], float], str] | None = None,
) -> list[str]:
    """Probe SAM and DeepSeek. The returned lines are status codes only."""
    caller = fetch or _fetch_status
    lines: list[str] = []
    sam = lookup(process, "SAM_API_KEY") or ""
    if sam:
        lines.append(caller("SAM", sam_probe_url(sam), {}, SAM_PROBE_TIMEOUT))
    else:
        lines.append("SAM  skipped, key absent")
    deepseek = lookup(process, "DEEPSEEK_API_KEY") or ""
    if deepseek:
        lines.append(caller(
            "DeepSeek",
            "https://api.deepseek.com/models",
            {"Authorization": "Bearer " + deepseek},
            DEEPSEEK_PROBE_TIMEOUT,
        ))
    else:
        lines.append("DeepSeek  skipped, key absent")
    lines.append("JEV  not probed. A decision package can contain company data, and this script does not send one.")
    return lines


def _fetch_status(name: str, url: str, headers: dict[str, str], timeout: float) -> str:
    try:
        req = urlrequest.Request(url, headers=headers, method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return f"{name}  http {resp.status}"
    except Exception as exc:
        code = getattr(exc, "code", None)
        return f"{name}  http {code if code is not None else 'failed'}"


def run_report(
    env_path: str,
    mode: str,
    http_status: str,
    body: str,
    process: MutableMapping[str, str] | None = None,
    fetch: Callable[[str, str, dict[str, str], float], str] | None = None,
) -> list[str]:
    """Presence, then health lines, then optional live probes. Values stay out of the lines."""
    environ: MutableMapping[str, str] = process if process is not None else os.environ
    try:
        file_values = load_env_file(env_path)
    except Exception:
        file_values = {}
        load_error = ".env  not read. The error text is omitted because it can contain a secret."
    else:
        load_error = ""
    lines = presence_lines(PRESENCE_NAMES, environ, file_values)
    if load_error:
        lines.append(load_error)
    lines.extend(format_health_report(http_status, body))
    if mode != "live":
        lines.append(
            "Live SAM and DeepSeek calls were not made. "
            "Re-run with -Live on the laptop to probe them. Output is still only a status code."
        )
        return lines
    fill_process_from_file(environ, file_values)
    lines.extend(live_lines(environ, fetch))
    return lines
