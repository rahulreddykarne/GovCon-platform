"""Build the repair summary from recorded checks, without inventing results."""
import ast
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent

fixes = [
    ("FV01", "P1", "Comparable full-order quote selection", "sourcing/records.py:280", "4 protection + 4 regression", "Unknown quantity/unit quotes need explicit human cost evidence.", "tests/test_fv01_pricing.py"),
    ("FV02", "P1", "SAM source, compatible agency and award-date bounds", "learning/award_matching.py:91", "2 protection + 4 regression", "Missing agency/date retains prior behavior; confirmation remains human.", "tests/test_fv02_award_attribution.py"),
    ("FV03", "P1", "Lease/source fence and checkpoint in service transaction", "workflow/preparation.py:150,163,172", "1 protection + 13 regression", "AI usage accounting remains independent of discarded work.", "tests/test_fv03_preparation_commits.py"),
    ("FV04", "P1", "Preserve existing cycles during automatic reviewer setup", "workflow/preparation.py:225", "4 protection + 4 regression", "Manual reassignment retains its existing behavior.", "tests/test_fv04_review_preservation.py"),
    ("FV05", "P1", "Review permission before supplier/file/quote writes", "cli.py:3354; sourcing/intake.py:22; sourcing/records.py:206", "11 protection + 3 regression", "actor=None remains the internal system-write path.", "tests/test_fv05_quote_permissions.py"),
    ("FV06", "P2", "Report surplus CSV fields inside per-row handler", "sourcing/records.py:127", "3 protection + 3 regression", "Malformed rows are skipped; valid rows still import.", "tests/test_fv06_catalog_rows.py"),
    ("FV07", "P2", "Translate workbook format errors and close workbook", "sourcing/records.py:176", "4 protection + 6 regression", "Content-addressed upload storage order is retained; rejected quotes are not recorded.", "tests/test_fv07_workbook_errors.py"),
    ("FV08", "P2", "Reject nonfinite Decimals before comparison", "sourcing/records.py:54", "10 protection + 11 regression", "Finite formatting, blank values and zero are preserved.", "tests/test_fv08_finite_numbers.py"),
    ("FV09", "P2", "Serialize document identity and CLI supplier creation", "sourcing/records.py:227; sourcing/intake.py:40; cli.py:3354", "6 protection + 7 regression", "Existing duplicates are retained; no data cleanup or schema change.", "tests/test_fv09_quote_repeats.py"),
    ("FV10", "P2", "Reset sentinels and age from current reopened cycle", "workflow/invalidation.py:84; collaboration/escalation.py:55,64", "2 protection + 2 regression", "Original assigned_at and deadline/daily reminder policies are retained.", "tests/test_fv10_review_cycles.py"),
    ("FV11", "P2", "Recover default alias commit from offline model cache", "enrich/embeddings.py:62,73,155", "5 protection + 3 regression", "Explicit revision/local hashing/fallback behavior is retained; no weight upgrade performed.", "tests/test_fv11_model_revision.py"),
]

final = json.loads((OUT / "FINAL-full.json").read_text())
reaudit = json.loads((OUT / "FINAL-feature-reaudit.json").read_text())
help_check = json.loads((OUT / "FINAL-cli-help.json").read_text())
model_check = json.loads((OUT / "FV11-local-model-final.json").read_text())
delta = json.loads((OUT / "delta-check.json").read_text())
assert delta["existing_signatures_and_decorators"] == "unchanged" and delta["existing_test_files_unchanged"] == 49
for result in (final, reaudit, help_check, model_check):
    assert result["pytest"]["exit_code"] == 0 and not result["baseline_regressions"]
assert not final["missing_baseline_tests"] and final["build"]["exit_code"] == 0

baseline_tree = ET.parse(ROOT / "docs/audit/feature_verification_2026-10-03/full-suite.xml")
final_tree = ET.parse(OUT / "FINAL-full.xml")
old = {(t.get("classname"), t.get("name")) for t in baseline_tree.findall(".//testcase")}
new_tests = [t for t in final_tree.findall(".//testcase") if (t.get("classname"), t.get("name")) not in old]
assert len(new_tests) == 112
stages = {}
for finding, *_ in fixes:
    result = json.loads((OUT / f"{finding}-full.json").read_text())
    assert result["pytest"]["exit_code"] == result["build"]["exit_code"] == 0
    assert not result["baseline_regressions"] and not result["missing_baseline_tests"]
    stages[finding] = result
cli_race = json.loads((OUT / "FV09-cli-race-full.json").read_text())
assert cli_race["pytest"]["exit_code"] == cli_race["build"]["exit_code"] == 0
assert not cli_race["baseline_regressions"] and not cli_race["missing_baseline_tests"]
stages["FV09 CLI concurrency follow-up"] = cli_race

before = json.loads((OUT / "before.json").read_text())
source_changes = []
for name, old_hash in before["hashes"].items():
    path = ROOT / name
    if name.startswith("src\\") and hashlib.sha256(path.read_bytes()).hexdigest() != old_hash:
        source_changes.append(name.replace("\\", "/"))
expected = {"src/govcon/sourcing/records.py", "src/govcon/learning/award_matching.py",
            "src/govcon/workflow/preparation.py", "src/govcon/sourcing/intake.py", "src/govcon/cli.py",
            "src/govcon/workflow/invalidation.py", "src/govcon/collaboration/escalation.py", "src/govcon/enrich/embeddings.py"}
assert set(source_changes) == expected, source_changes

original = (ROOT / "docs/FEATURE_VERIFICATION_2026-10-03.md").read_text(encoding="utf-8")
matrix = original.split("## Feature verification matrix", 1)[1].split("## Readiness checklist", 1)[0]
rows = [[cell.strip() for cell in line.split("|")[1:-1]] for line in matrix.splitlines() if line.startswith("| ")]
rows = rows[2:]
assert len(rows) == 95
affected = {
    "Review reminders/deadline escalation": ["FV10"],
    "Pricing intelligence": ["FV01", "FV05", "FV07", "FV08", "FV09"],
    "Review completion/quorum/approval": ["FV03", "FV04", "FV10"],
    "Automated preparation": ["FV03", "FV04"],
    "Task listing/status/retry/cancel": ["FV03"],
    "Catalog import/upsert": ["FV06", "FV08"],
    "CSV/XLSX supplier quote intake": ["FV05", "FV07", "FV08", "FV09"],
    "Manual supplier quote totals": ["FV05", "FV08"],
    "CLI sourcing role restriction": ["FV05"],
    "Automatic outcome suggestions/confirm/dismiss": ["FV02"],
    "Local embeddings/freshness": ["FV11"],
    "Recommendation categories/persistence": ["FV11"],
    "Current/expired sourcing evidence": ["FV01", "FV09"],
    "Supplier intelligence": ["FV05", "FV09"],
    "AI document quote extraction": ["FV05", "FV09"],
    "Solicitation summary/context coverage": ["FV03"],
    "Decision rule bundles/package/history": ["FV03"],
    "JEV/rules fallback": ["FV03"],
    "Requirements extraction/reconciliation": ["FV03"],
    "Compliance matrix/coverage/findings": ["FV03"],
    "Amendment invalidation": ["FV10"],
    "Manual reviewer assignment/context": ["FV04"],
    "Comments and validation/consolidation": ["FV04", "FV10"],
    "Pursuit start/deduplication": ["FV03", "FV04"],
    "Owner workflow settings": ["FV03", "FV04", "FV10"],
    "Worker lease/claim/checkpoint engine": ["FV03"],
    "Watchlist embedding profiles": ["FV11"],
    "Similar opportunities / heuristic fallback": ["FV11"],
    "Learning analytics/snapshots": ["FV02"],
}
related = {
    "CLI command discovery", "Inbox filtering and actions", "Guarded auto-pursue", "Pipeline/workspace views",
    "In-app notifications/read/acknowledge", "Durable notification email tasks", "Market intelligence",
    "AI gateway/provider adapters/budgets", "Compliance source inventory", "Clause library/validators",
    "Evidence and human overrides", "Proposal coverage/preflight/readiness", "Job list/manual run/scheduled chains",
    "Durable proposal drafting", "Proposal versions/status", "Proposal red-team", "Final approve/return/cancel",
    "DOCX/XLSX/ZIP export", "Submission assemble/package/checklist", "Submission instructions/email draft",
    "Commercial correction / manual confirmation", "Supplier directory/provenance", "RFQ draft",
    "Owner AI-sharing authorization", "Manual outcome/history/correction", "Explainable ranking",
    "MCP read tool contract", "MCP write tool contract", "Installed application portability",
}
old_status = {r[0]: r[-1] for r in rows}
for row in rows:
    if row[0] in affected:
        labels = ", ".join(affected[row[0]])
        row[3] = f"Final feature recheck + full suite; {labels} regression/protection cases; caller trace in repair log."
        if "FV11" in affected[row[0]]:
            row[3] += " Real offline cached model and pgvector/profiles/recommendation persistence passed."
        row[-1] = "WORKS"
    elif row[0] in related:
        row[3] = "Final feature recheck/full suite: " + row[3]
        row[3] = row[3].replace("Preparation's later defects are separate.", "Preparation regressions FV03/FV04 pass.")
    elif row[-1] == "WORKS":
        row[3] = "Full suite rerun; original execution/trace retained: " + row[3]
assert sum(r[-1] == "WORKS" for r in rows) == 85
assert sum(r[-1] == "UNVERIFIED" for r in rows) == 10

passed = int(final["pytest"]["tests"]) - int(final["pytest"]["skipped"])
lines = ["# Feature repair results — 2026-10-03", "",
    "All 11 reported findings are FIXED, in P1 then P2 order; the audit found no P0. Each fix has passing pre-change behavior protections, a recorded failing regression, the smallest scoped change, caller review and a full-suite/build gate. No baseline passing test regressed.", "",
    "The final feature matrix has 85 WORKS and the same 10 UNVERIFIED external/browser integration rows. No feature got worse. WORKS is limited to the named cases and run/test/trace evidence; it does not certify live integrations.", "",
    "Existing user edits were preserved. Eight production Python files changed relative to the before-fix snapshot; no existing function signature, API response format, schema, manifest or config was changed. No deployment, paid model/government request, mail or production-data mutation was performed.", "",
    "Snapshot verification: all 49 pre-existing test files and existing function signatures/decorators are unchanged. [Delta check](audit/fixes_2026-10-03/delta-check.json) records the eight edited files and one new private preparation helper.", "",
    "## Baseline comparison", "", "| Check | Audit baseline | Final | Comparison |", "| --- | --- | --- | --- |",
    f"| Full tests | 1,160 passed; 1 skipped; 0 failed/errors | {passed:,} passed; {final['pytest']['skipped']} skipped; {final['pytest']['failures']} failed; {final['pytest']['errors']} errors | All 1,160 baseline passes present and passing; 112 added cases |",
    "| Typecheck | `No module named mypy` | `No module named mypy` | Same unavailable check |",
    "| Linter | `No module named ruff` | `No module named ruff` | Same unavailable check |",
    "| Build/installed artifact | Installed-wheel bootstrap/lifecycle passed | Wheel build exit 0; installed-wheel test and lifecycle passed | Pass retained |", "",
    f"Final suite JUnit time: {final['pytest']['time']} seconds. The one skip is the unchanged live SAM test (`SAM_API_KEY is not set`). Every stage logs the attempted mypy/ruff commands and their actual missing-module output.", "",
    "## Fix summary", "", "Original audit locations are relative to `src/govcon/`; implementation locations can shift after earlier fixes. The full ordered plan, caller map, per-fix results and blast-radius reviews are in [FEATURE_FIXES_2026-10-03.md](FEATURE_FIXES_2026-10-03.md).", "",
    "| Finding | Fix | Tests added | Status | Risk notes |", "| --- | --- | --- | --- | --- |"]
for finding, severity, fix, location, tests, risk, filename in fixes:
    lines.append(f"| {finding} {severity} — `{location}` | {fix} | [{tests}](../{filename}) | FIXED | {risk} |")
lines += ["", "112 tests added: 52 behavior protections and 60 regression cases. The 59 regressions for the original findings were observed failing for their reported causes before their fixes. The additional quote caller regression reproduced the concurrent CLI supplier-creation error before its lock-order fix. The original audit probes intentionally assert pre-fix defects; they remain evidence and were not weakened to make them pass.", "",
    "## Per-fix full checks", "", "| Stage | Passed | Skipped | Baseline regressions/missing | Wheel build | Typecheck/lint |", "| --- | --- | --- | --- | --- | --- |"]
for finding, result in stages.items():
    p = result["pytest"]
    stage_file = "FV09-cli-race-full.json" if finding == "FV09 CLI concurrency follow-up" else f"{finding}-full.json"
    lines.append(f"| [{finding}](audit/fixes_2026-10-03/{stage_file}) | {int(p['tests'])-int(p['skipped']):,} | {p['skipped']} | 0 / 0 | exit 0 | unavailable, unchanged |")
lines += [f"| [Final](audit/fixes_2026-10-03/FINAL-full.json) | {passed:,} | 1 | 0 / 0 | exit 0 | unavailable, unchanged |", "",
    "## Final feature recheck", "",
    f"The separate selected re-audit passed {reaudit['pytest']['tests']} tests with zero failures/errors/skips. All 96 registered CLI command help calls passed again. The real cached model passed offline with disposable PostgreSQL, including upgrading old unversioned metadata, 384 finite vectors, repeat skips, medical-supply similarity, watchlist/pursued profiles, minimum-win empty behavior and five persisted recommendations with stable repeat IDs.", "",
    "Rows for changed behavior and its related callers were rechecked using executed feature tests plus caller traces; the remaining inventory rows retain their prior evidence and have unchanged full-suite checks. The external/browser rows remain UNVERIFIED. Evidence: [feature recheck](audit/fixes_2026-10-03/FINAL-feature-reaudit.json), [96 command help calls](audit/fixes_2026-10-03/FINAL-cli-help.json), [real model](audit/fixes_2026-10-03/local-model-results.json).", "",
    "| Feature | Entry point | Expected behavior / touched state and cases | Verified by | Status |", "| --- | --- | --- | --- | --- |"]
lines += ["| " + " | ".join(row) + " |" for row in rows]
lines += ["", "## Readiness limits", "",
    "- PASS: baseline preservation, all reported regression cases, per-fix/full final tests, build/installed artifact, observed role denial, fenced preparation writes, review-cycle preservation, finite sourcing inputs, repeat/concurrent quote identity, award filtering and default-model provenance.",
    "- NOT VERIFIED: mypy/ruff (not installed/configured in the baseline), live SAM/DIBBS/USAspending/entity access, paid AI/JEV/evaluation, SMTP delivery, browser JavaScript/HTMX/accessibility and external MCP stdio round trip. These retain their previous audit status.",
    "- NOT VERIFIED: deployment/rolling migration, backup/restore, production-sized performance and multi-host soak. No model weights were upgraded; cached-revision changes were simulated and the actual default cached revision was exercised offline.",
    "- Existing stored duplicate quotes are not deleted. Rejected upload bytes may remain in the existing content-addressed store; the quote transaction is rolled back. Explicit revision and unavailable-cache fallback behavior was preserved.", "",
    "The one selected-suite AI-comment failure occurred both with FV03 restored and reverted. Running its existing prompt-registry prerequisite resolved it (125 predecessor passes; 139 restored passes). Diagnostic logs are retained; full-suite baseline checks never regressed.", ""]
(ROOT / "docs/FEATURE_FIX_RESULTS_2026-10-03.md").write_text("\n".join(lines), encoding="utf-8")
summary = {"findings_fixed": 11, "new_tests": 112, "behavior_protections": 52, "regressions": 60,
           "final": final, "feature_reaudit": reaudit, "source_files_changed": sorted(source_changes),
           "snapshot_check": delta,
           "matrix_counts": {"WORKS": 85, "UNVERIFIED": 10}, "before_status": old_status,
           "affected_or_related_features": sorted(set(affected) | related)}
(OUT / "final-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
plan_path = ROOT / "docs/FEATURE_FIXES_2026-10-03.md"
plan = plan_path.read_text(encoding="utf-8")
plan = plan.replace("`start_pursuit`/`queue_preparation`", "`create_or_get_pursuit`/`queue_preparation`")
plan = plan.replace("`review_sessions.reopen_for_amendment`, `commercial` correction; exposed by ingestion/scheduler, review CLI, commercial CLI.",
                    "`review_sessions.apply_material_amendment_reopen` (compliance pipeline/review CLI), `commercial.invalidate_commercial_decisions` (MCP `op_update_pursuit` pre-submission edits); exposed by ingestion/scheduler, review CLI and MCP. Submitted commercial corrections retain their separate append-only path.")
plan = plan.replace("Provider `model_version` → `_model_id`", "Provider `model_version` → `refresh_recommendations` and `_model_id`")
plan = plan.replace("Serialize quote insertion by opportunity and reuse the same document identity,", "Serialize CLI supplier creation and quote insertion by opportunity, then reuse the same document identity,")
plan = plan.replace("`record_quote` → `receive_quote_file`, web `workspace_add_quote`, quote handler `_extract_publish`; intake exposed by quote CLI/web.",
                    "`record_quote` → `receive_quote_file`, web `workspace_add_quote`, quote handler `_extract_publish`; CLI `sourcing_add_quote` (Typer command registry) guards before supplier creation/intake; web guards before supplier creation/intake too.")
plan = plan.replace("- Full verification: running.", f"- Full verification: {passed:,} passed / 1 skipped; zero failures/errors and no missing/regressed passing baseline cases. Wheel build exit 0; mypy/ruff unchanged and unavailable.")
plan += f"\n## Final verification\n\nThe independent final gate passed {passed:,} tests with the one unchanged SAM skip; wheel build passed; mypy/ruff remain unavailable. The separate feature re-audit passed {reaudit['pytest']['tests']} cases, all 96 CLI help calls passed, and the real offline model check passed. No feature status worsened. The 95-row inventory is now 85 WORKS and the same 10 UNVERIFIED. See [FEATURE_FIX_RESULTS_2026-10-03.md](FEATURE_FIX_RESULTS_2026-10-03.md) for the summary, baseline comparison and matrix.\n"
plan_path.write_text(plan, encoding="utf-8")
print(json.dumps({"findings_fixed": 11, "new_tests": 112, "passed": passed,
                  "matrix": summary["matrix_counts"], "source_files_changed": len(source_changes)}))
