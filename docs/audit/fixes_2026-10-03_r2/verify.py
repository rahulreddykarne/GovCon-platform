"""Record isolated repair checks (round 2) without touching live integrations.

usage: python verify.py LABEL [--full | pytest args...]
Full runs are compared test-by-test against BASELINE.xml (written by the
first `BASELINE --full` run) and lint/typecheck error counts against
BASELINE-lint.log / BASELINE-typecheck.log.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
label = sys.argv[1]
full = sys.argv[2:] == ["--full"]
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
env = os.environ.copy()
env.update(DATABASE_URL="postgresql+psycopg://govcon:govcon@127.0.0.1:55432/govcon",
           GOVCON_TEST_NO_DB="0", PYTHONUTF8="0", SAM_API_KEY="", COMPANY_SAM_API_KEY="",
           ANTHROPIC_API_KEY="", OPENAI_API_KEY="", DEEPSEEK_API_KEY="", JEV_API_KEY="",
           SMTP_HOST="", SMTP_USERNAME="", SMTP_PASSWORD="", EMAIL_NOTIFICATIONS_ENABLED="false")
results = {}


def run(name, args):
    log = OUT / f"{label}-{name}.log"
    with log.open("wb") as stream:
        completed = subprocess.run(args, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    results[name] = {"exit_code": completed.returncode, "log": log.name}
    return completed.returncode


def diag_lines(path, pattern):
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    # Drop line:col so pure line shifts from an edit don't look like new errors.
    return {re.sub(r":\d+(:\d+)?:", ":", line).strip() for line in text.splitlines() if re.search(pattern, line)}


with tempfile.TemporaryDirectory(prefix="govcon-fix-check-") as temp:
    env["DATA_DIR"] = str(Path(temp) / "data")
    xml = OUT / f"{label}.xml"
    args = [VENV_PY, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", f"--junitxml={xml}"]
    if not full:
        args.extend(sys.argv[2:])
    failed = run("pytest", args)
    if xml.exists():
        tree = ET.parse(xml)
        suite = tree.find(".//testsuite")
        results["pytest"].update({k: suite.get(k) for k in ("tests", "failures", "errors", "skipped", "time")})
        base_xml = OUT / "BASELINE.xml"
        if base_xml.exists() and label != "BASELINE":
            baseline = ET.parse(base_xml)
            old = {(t.get("classname"), t.get("name")) for t in baseline.findall(".//testcase")
                   if not any(t.find(k) is not None for k in ("skipped", "failure", "error"))}
            current = {(t.get("classname"), t.get("name")): t for t in tree.findall(".//testcase")}
            regressions = [key for key in old if key in current and
                           any(current[key].find(k) is not None for k in ("failure", "error", "skipped"))]
            missing = sorted(old - current.keys()) if full else []
            results["baseline_regressions"] = sorted(regressions)
            results["missing_baseline_tests"] = missing
            failed = failed or bool(regressions or missing)
    if full:
        run("typecheck", ["uvx", "mypy", "--python-executable", VENV_PY, "src/govcon"])
        run("lint", ["uvx", "ruff", "check", "--output-format", "concise", "src", "tests"])
        failed = run("build", [VENV_PY, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                               "--wheel-dir", str(Path(temp) / "wheel"), "."]) or failed
        for kind, pat in (("typecheck", r": error:"), ("lint", r"^\S+\.py:\d+:\d+: [A-Z]+\d+")):
            now = diag_lines(OUT / f"{label}-{kind}.log", pat)
            results[kind]["count"] = len(now)
            m = re.search(r"Found (\d+) error", (OUT / f"{label}-{kind}.log").read_text(encoding="utf-8", errors="replace"))
            results[kind]["total"] = int(m.group(1)) if m else 0
            if label != "BASELINE":
                base = diag_lines(OUT / f"BASELINE-{kind}.log", pat)
                results[kind]["baseline_count"] = len(base)
                results[kind]["new_vs_baseline"] = sorted(now - base)
                bm = re.search(r"Found (\d+) error", (OUT / f"BASELINE-{kind}.log").read_text(encoding="utf-8", errors="replace"))
                results[kind]["baseline_total"] = int(bm.group(1)) if bm else 0
                failed = failed or results[kind]["total"] > results[kind]["baseline_total"]
                failed = failed or bool(now - base)
    (OUT / f"{label}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"stage": label, **results}, indent=1)[:6000])
    if results["pytest"]["exit_code"]:
        print((OUT / results["pytest"]["log"]).read_bytes().decode("utf-8", errors="replace")[-5000:])
    sys.exit(1 if failed else 0)
