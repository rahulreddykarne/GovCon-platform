"""Parser for the laptop live-check script. No network and no real .env."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from govcon.envfile import (
    credential_state,
    format_health_report,
    load_env_file,
    parse_env_text,
    resolved_value,
    run_report,
)

_SAMPLE = (
    "\ufeffSAM_API_KEY=\"sam-sample-value\"   \r\n"
    "# comment line\n"
    "DEEPSEEK_API_KEY=deepseek-from-file # inline comment\r\n"
    "JEV_API_KEY = 'jev-sample-value'\r\n"
    "SAM_API_KEY=second-sam-value\n"
    "export ANTHROPIC_API_KEY=anthropic-sample\n"
    "OPENAI_API_KEY=\n"
    "SMTP_HOST=smtp.example.test   \n"
    "QUOTED_HASH=\"ab#cd\"\n"
    "BLANK=\"\"\n"
)


def test_sample_env_handles_quotes_bom_crlf_comments_and_one_key() -> None:
    values = parse_env_text(_SAMPLE)
    assert values["SAM_API_KEY"] == "second-sam-value"
    assert list(values).count("SAM_API_KEY") == 1
    assert values["DEEPSEEK_API_KEY"] == "deepseek-from-file"
    assert values["JEV_API_KEY"] == "jev-sample-value"
    assert values["ANTHROPIC_API_KEY"] == "anthropic-sample"
    assert values["OPENAI_API_KEY"] == ""
    assert values["SMTP_HOST"] == "smtp.example.test"
    assert values["QUOTED_HASH"] == "ab#cd"
    assert values["BLANK"] == ""
    assert "\ufeffSAM_API_KEY" not in values


def test_utf8_sig_file_and_process_env_precedence(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(_SAMPLE.encode("utf-8-sig"))
    values = load_env_file(path)
    process = {"DEEPSEEK_API_KEY": "from-process-deepseek", "SMTP_HOST": "   "}
    assert resolved_value("DEEPSEEK_API_KEY", process, values) == "from-process-deepseek"
    assert resolved_value("SAM_API_KEY", process, values) == "second-sam-value"
    assert resolved_value("JEV_API_KEY", process, values) == "jev-sample-value"
    assert credential_state("SMTP_HOST", process, values) == "absent"
    assert credential_state("OPENAI_API_KEY", process, values) == "absent"
    assert credential_state("ANTHROPIC_API_KEY", {}, values) == "present"
    assert load_env_file(tmp_path / "missing.env") == {}


def test_report_prints_presence_and_one_line_per_health_check(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_SAMPLE, encoding="utf-8")
    body = """
    {"status":"ok","checks":[
      {"name":"database","status":"ok","detail":"reachable (1 ms)"},
      {"name":"web","status":"ok","detail":"heartbeat 3s ago"},
      {"name":"worker","status":"ok","detail":"heartbeat 4s ago"},
      {"name":"source:sam","status":"stale","detail":"last success from local history"}
    ]}
    """
    lines = run_report(str(path), "local", "200", body, process={"DEEPSEEK_API_KEY": "from-process-deepseek"})
    text = "\n".join(lines)
    assert "SAM_API_KEY  present" in lines
    assert "DEEPSEEK_API_KEY  present" in lines
    assert "JEV_API_KEY  present" in lines
    assert "OPENAI_API_KEY  absent" in lines
    assert lines.count("SAM_API_KEY  present") == 1
    assert "GET /health  200  status=ok" in lines
    assert "  database  ok  reachable (1 ms)" in lines
    assert "  web  ok  heartbeat 3s ago" in lines
    assert "  worker  ok  heartbeat 4s ago" in lines
    assert "  source:sam  stale  last success from local history" in lines
    for banned in ("Count", "Length", "Rank", "SyncRoot", "sam-sample-value", "second-sam-value", "jev-sample-value", "from-process-deepseek", "deepseek-from-file"):
        assert banned not in text
    assert any(line.startswith("Live SAM and DeepSeek calls were not made") for line in lines)


def test_health_object_shape_and_unreadable_body() -> None:
    lines = format_health_report("200", '{"status":"ok","checks":{"database":{"status":"ok","detail":"reachable"}}}')
    assert lines == ["GET /health  200  status=ok", "  database  ok  reachable"]
    skipped = format_health_report("200", '{"status":"ok","checks":{"Count":1,"Length":1,"database":"ok"}}')
    assert "Count" not in "\n".join(skipped)
    assert "  database  ok" in skipped
    assert format_health_report("unavailable", "")[0].startswith("GET /health  unavailable")
    unreadable = format_health_report("200", "not-json")[0]
    assert "not-json" not in unreadable
    assert "unreadable" in unreadable


def test_live_mode_uses_file_key_without_printing_it(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('SAM_API_KEY="sam-sample-value"\n', encoding="utf-8")
    seen: list[str] = []

    def fetch(name: str, url: str, headers: dict[str, str], timeout: float) -> str:
        seen.append(url)
        seen.append(str(timeout))
        seen.extend(headers.values())
        return f"{name}  http 200"

    lines = run_report(str(path), "live", "unavailable", "", process={}, fetch=fetch)
    text = "\n".join(lines)
    assert "SAM_API_KEY  present" in lines
    assert "SAM  http 200" in lines
    assert "DeepSeek  skipped, key absent" in lines
    assert any(line.startswith("JEV  not probed") for line in lines)
    parsed = urlparse(seen[0])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.sam.gov"
    assert parsed.path == "/opportunities/v2/search"
    assert query["limit"] == ["1"]
    assert query["api_key"] == ["sam-sample-value"]
    assert query["postedFrom"][0].count("/") == 2
    assert query["postedTo"][0].count("/") == 2
    assert float(seen[1]) >= 90
    assert "sam-sample-value" not in text
    assert "api.sam.gov" not in text


def test_live_check_script_is_a_file_not_python_c() -> None:
    root = Path(__file__).resolve().parents[1]
    ps1 = (root / "scripts/windows/Invoke-GovConLiveChecks.ps1").read_text(encoding="utf-8")
    helper = root / "scripts/windows/live_checks.py"
    code_lines = [line for line in ps1.splitlines() if not line.lstrip().startswith("#")]
    assert all(" -c " not in line for line in code_lines)
    assert "live_checks.py" in ps1
    compile(helper.read_text(encoding="utf-8"), str(helper), "exec")
