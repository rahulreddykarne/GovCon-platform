"""Record isolated repair checks without sending to live integrations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
label = sys.argv[1]
full = sys.argv[2:] == ["--full"]
env = os.environ.copy()
env.update(DATABASE_URL="postgresql+psycopg://govcon:govcon@127.0.0.1:5432/govcon",
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

with tempfile.TemporaryDirectory(prefix="govcon-fix-check-") as temp:
    env["DATA_DIR"] = str(Path(temp) / "data")
    xml = OUT / f"{label}.xml"
    args = [sys.executable, "-m", "pytest", "-q", "-rs", f"--junitxml={xml}"]
    if not full:
        args.extend(sys.argv[2:])
    failed = run("pytest", args)
    if xml.exists():
        tree = ET.parse(xml)
        suite = tree.find(".//testsuite")
        results["pytest"].update({k: suite.get(k) for k in ("tests", "failures", "errors", "skipped", "time")})
        baseline = ET.parse(ROOT / "docs/audit/feature_verification_2026-10-03/full-suite.xml")
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
        run("typecheck", [sys.executable, "-m", "mypy", "src/govcon"])
        run("lint", [sys.executable, "-m", "ruff", "check", "src", "tests"])
        failed = run("build", [sys.executable, "-m", "pip", "wheel", "--no-deps",
                               "--no-build-isolation", "--wheel-dir", str(Path(temp) / "wheel"), "."]) or failed
    (OUT / f"{label}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"stage": label, **results}))
    if results["pytest"]["exit_code"]:
        print((OUT / results["pytest"]["log"]).read_bytes().decode("utf-8", errors="replace")[-7000:])
    sys.exit(1 if failed else 0)
