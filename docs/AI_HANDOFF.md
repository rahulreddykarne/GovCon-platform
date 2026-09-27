# AI handoff

Append a new entry after each implementation session. Do not rewrite earlier entries. Do not start the next phase in the same session that finishes the current one.

## 2026-09-26 23:45 UTC — PHASE_12_MCP

- Agent/model identity: Cursor cloud agent, model `composer-2.5`
- Datetime (UTC): 2026-09-26 23:45 UTC
- Phase/task: PHASE_12_MCP (master §18)
- Branch: `cursor/phase-12-mcp-d442`
- Base: `main` at `88caffa` (Phase 11 squash merge)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/15 (draft)

### Files changed

- `pyproject.toml` — add `fastmcp>=4.0`
- `src/govcon/config.py` — optional `mcp_actor_email`
- `src/govcon/mcp/__init__.py`, `serialize.py`, `context.py`, `operations.py`, `server.py`
- `src/govcon/cli.py` — `govcon mcp serve`
- `tests/test_mcp.py`
- `IMPLEMENTATION_STATUS.md`, `DECISIONS.md` (ADR-043), `SPEC_DEVIATIONS.md` (DEV-007), this file

### What shipped

- FastMCP stdio server exposing all §18 read/write tools as thin wrappers over existing services (matching, intelligence, decision, compliance, proposals, submissions, collaboration).
- Compact structured responses with description truncation unless `include_full_description` / `include_full_text` / `include_requirement_text` is set.
- Structured error envelope (no stack traces); `audit.scrub` on outputs; match dismissal requires `confirm=true`; submission confirmation never auto-submits.
- Actor resolution for write tools via `actor_email`, `MCP_ACTOR_EMAIL`, `mcp_actor_email`, or first active owner.
- `similar_opportunities` heuristic (NSN/PSC/agency) until Phase 13; `learning_summary` reads `outcome_feedback` until Phase 15 analytics.

### ADRs / DECISIONS touched

- ADR-043 (new). DEV-007 in `SPEC_DEVIATIONS.md`.

### Migrations

None.

### Tests

Local PostgreSQL 16 + pgvector on `localhost:5432` (installed on the VM).

- Dependency check before coding: `pytest` on `main` after pull → 207 passed, 1 skipped (Phases 0–11 green).
- `pytest tests/test_mcp.py` → 6 passed (includes Phase 12 acceptance e2e workflow).
- `pytest` → 213 passed, 1 skipped.

### Acceptance criteria (§18)

1. Example e2e interaction through MCP against local DB — `test_acceptance_e2e_mcp_workflow` lists new matches closing within 7 days, returns price history, explains bid candidate missing information, moves match to `reviewing`, and surfaces compliance gaps.
2. All 15 read + 13 write tools registered — `test_mcp_registers_all_phase12_tools`.
3. Compact output + truncation — `test_compact_opportunity_truncates_description_and_scrubs_secrets`.
4. Structured errors — `test_truncate_text_and_failure_shape` and dismiss-without-confirm path.
5. No secret leakage in serialized opportunity links — scrub test.
6. Destructive dismiss requires explicit intent — `test_dismiss_match_requires_confirm`.
7. Write tool echoes changed record — `test_record_outcome_persists_feedback`.

### VERIFY outcomes

`PHASE_12_MCP.md` has no `⚠️ VERIFY` items. FastMCP 4.x `@mcp.tool` + stdio `mcp.run()` verified against installed `fastmcp==4.0.10`.

### Known problems

- MCP transport is stdio only in this phase (HTTP/SSE deferred).
- `similar_opportunities` is heuristic, not embedding-based (Phase 13).
- `learning_summary` is a table summary, not Phase 15 analytics.
- Live SAM vendor fetch in `vendor_profile` still requires `SAM_API_KEY` when cache is cold.

### Unfinished work

None for Phase 12 scope. Phase 13 semantic search and Phase 14 web UI are not started.

### Recommended next task

**Phase 13 — Semantic search & recommendations (`PHASE_13_SEMANTIC_SEARCH.md`).** Start only after this draft PR passes the Spec/QA gate and merges. Replace the `similar_opportunities` heuristic with pgvector-backed recommendations.

## 2026-09-26 23:20 UTC — PHASE_10_COLLABORATIVE_REVIEW

- Agent/model identity: Cursor cloud agent, model `codex-5.3`
- Datetime (UTC): 2026-09-26 23:20 UTC
- Phase/task: `PHASE_10_COLLABORATIVE_REVIEW.md` only (master §16 + §24A collaboration requirements)
- Branch: `cursor/phase-10-collaborative-review-7021`
- Base: `main` at `9ee76af` (Phase 9 merge tip)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/13 (draft)

### Files changed

- `src/govcon/collaboration/assignments.py`
- `src/govcon/collaboration/comments.py`
- `src/govcon/collaboration/ai_comment_review.py`
- `src/govcon/collaboration/review_sessions.py`
- `src/govcon/collaboration/__init__.py`
- `src/govcon/cli.py` (new `govcon review ...` command group)
- `src/govcon/config.py` (review policy trigger settings)
- `src/govcon/ai/schemas.py` (Phase 10 structured schemas)
- `src/govcon/prompts/deepseek/reviewer_comment_validation_v1.md`
- `src/govcon/prompts/deepseek/consolidated_review_v1.md`
- `tests/test_collaborative_review.py` (new DB-backed acceptance coverage)
- `tests/test_http_prompts.py` (Phase 10 active prompts)
- `IMPLEMENTATION_STATUS.md`, `DECISIONS.md`, `SPEC_DEVIATIONS.md`, `docs/AI_HANDOFF.md`

### What shipped

- Multi-user collaborative review workspace over shared AI decision package.
- Reviewer assignment lifecycle (`assign`, `start`, `reassign`, `complete`, `reopen`) with notifications and audit events.
- Threaded append-first comments with attribution/timestamp preservation.
- AI sidecar validation for substantive comments using `reviewer_comment_validation.v1`; human text is never overwritten.
- Conditional/single/dual quorum policy recomputation with configurable triggers and second-review notifications.
- Consolidated review synthesis after quorum (`consolidated_review.v1`) plus final JEV collaborative synthesis bundle run.
- Human-only approval gate (`approve_to_bid | return_for_review | no_bid`) with optimistic concurrency and authorized override path.
- Material-amendment reopen path implemented (`review_reopen_required` finding from Phase 9 reopens completed reviews).
- CLI operations for assignment, comment, completion, workspace context, approval, and amendment-driven reopen.

### Tests

- Dependency gate before implementation:
  - `python3 -m pytest tests/test_cli_and_schema.py tests/test_attachments_analysis.py tests/test_decision_engine.py tests/test_compliance.py -q` → pass
- Phase 10 focused:
  - `python3 -m pytest tests/test_collaborative_review.py tests/test_http_prompts.py -q` → 10 passed
- Full regression:
  - `python3 -m pytest` → 180 passed, 1 skipped

### Acceptance criteria check (Phase 10 + §24A)

1. Two users can review same opportunity in parallel — covered.
2. Comments are attributed + timestamped — covered.
3. AI opinion generated for substantive comments — covered.
4. AI opinion never overwrites human comments — covered.
5. Reviewers complete independently — covered.
6. Approval blocked until quorum unless authorized override — covered.
7. Consolidated review exposes agreements/disagreements/open issues/evidence-needed fields — covered.
8. `approved_to_bid` requires authorized approver/owner action — covered.
9. One-review quorum can progress without optional second reviewer — covered.
10. Dual/conditional quorum blocks until satisfied or overridden — covered.
11. Reviewer-requested second review trigger is honored + auditable — covered.
12. Material amendment after review reopens review workflow (DEV-004 follow-through) — covered.

### VERIFY outcomes

- `PHASE_10_COLLABORATIVE_REVIEW.md` includes no explicit `⚠️ VERIFY` interface checks beyond dependency verification; dependencies 0/7/8/9 were re-run and passed before coding.

### Known problems

- No live `DEEPSEEK_API_KEY` / `JEV_API_KEY` in this environment; AI validation/consolidation tests mock providers and JEV synthesis routes to fallback behavior already validated in Phase 8.

### Unfinished work

- No known unfinished work within Phase 10 scope.
- Phase 11+ and Phase 14 UI intentionally not started.

### Recommended next task

`PHASE_11_PROPOSAL_SUBMISSION.md` (do not start until this Phase 10 draft PR passes Spec/QA gate and merges).

## 2026-09-26 22:30 UTC — PHASE_09_COMPLIANCE

- Agent/model identity: Cursor cloud agent, model `claude-opus-5-5`
- Datetime (UTC): 2026-09-26 22:30 UTC
- Phase/task: PHASE_09_COMPLIANCE (master §15). Resumed after an interrupted run; the in-progress work was kept and completed.
- Branch: `cursor/phase-09-compliance-e5e4`
- Base: `main` at `3b6eaab` (Phase 8, pull request #11)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/12 (draft)

### Files changed

- `alembic/versions/d9a4c1e7b209_phase9_compliance_subsystem.py` (new migration)
- `src/govcon/models.py`, `src/govcon/config.py`, `src/govcon/collaboration/users.py` (`override_compliance` permission)
- `src/govcon/compliance/`: `records.py`, `text.py`, `schemas.py`, `inventory.py`, `extractor.py`, `reconciler.py`, `clauses.py`, `data/clause_library_v1.json`, `conflicts.py`, `deterministic.py`, `matrix.py`, `validator.py`, `amendments.py`, `red_team.py`, `metrics.py`, `proposal_coverage.py`, `submission_preflight.py`, `regression.py`, `pipeline.py`
- `src/govcon/ai/structured.py` (shared structured-prompt runner), `src/govcon/ai/schemas.py` (registers compliance schemas)
- `src/govcon/prompting/renderer.py` (required variables, user-context data blocks), `registry.py` (gated `activate_prompt`, `rollback_prompt`), `evaluation.py` (activation gate)
- `src/govcon/prompts/deepseek/requirement_extraction_{a,b}_v1.md`, `amendment_analysis_v1.md`, `src/govcon/prompts/compliance/*.md` (9 prompts, production bodies from §40/§39.5, status active)
- `src/govcon/enrich/extract.py` (DOCX tables, per-page PDF text)
- `src/govcon/cli.py` (`govcon compliance …`, gated `prompts activate`, `prompts rollback`)
- `tests/test_compliance.py`, `tests/fixtures/compliance/` (3 cases + `baseline_metrics.json`), `tests/test_http_prompts.py`
- `tests/test_dibbs.py`: strip ANSI styling before the `--help` substring check. This pre-existing test failed on GitHub Actions (also on `main` after #11) because Rich forces styled output there.
- `pyproject.toml` (package data), `.env.example`, `IMPLEMENTATION_STATUS.md`, `SPEC_DEVIATIONS.md`, `DECISIONS.md`, this file

### What shipped

- Document inventory with every §15.2 field, and detection of missing/duplicate/re-versioned/amended/failed/unreadable sources; blocking problems make runs `incomplete` and raise findings.
- Pass A / Pass B AI extraction with different prompts and context strategies (Pass B provider/model and high-value escalation are configurable), a deterministic mandatory-language/table scanner, structural amendment-acknowledgment requirements, and citation verification.
- Conservative reconciliation that never drops a candidate; A/B disagreement → `needs_review`; the AI reconciler only adds flags.
- 19 deterministic validators with the `pass|fail|unknown` + reason + evidence + version contract.
- Single status gate: evidence/validator required for `satisfied`; deterministic failures block AI claims (visible `ai_claim_blocked` finding); `unknown` never collapses; critical requires two methods; stale never green; audited, role-checked, versioned overrides.
- Clause library (24 verified seeds) with verification questions; unknown/re-dated/alternate clauses flagged.
- Conflict detection with version-rank precedence; supersession or ambiguous → `needs_review`.
- Amendment invalidation: stale requirements and proposal sections, "COMPLIANCE STATUS CHANGED" alert, sourcing/pricing flags, JEV `compliance_and_amendment` + `bid_decision` re-runs, review-reopen flag.
- Red team (deterministic checklist + `compliance_red_team` prompt) persisted to `compliance_findings` with certainty.
- JEV routing through Phase 8 `run_decision_bundle` (can add blockers, never clear them).
- Count-based coverage by status and category; `false_satisfied_detected` self-check.
- Proposal coverage against a selected proposal version (substance + counts; writer mapping alone never counts); gaps are blocking findings.
- Submission pre-flight (§15.18 checklist) and the `ready_to_submit` gate with audited override.
- Replayed compliance benchmark + release gate (`govcon compliance benchmark`), and gated prompt activation/rollback.

### ADRs / DECISIONS touched

ADR-030 through ADR-037 (new). Earlier ADRs unchanged. SPEC_DEVIATIONS Phase 9: DEV-003 (UI → data/CLI), DEV-004 (review reopen flagged for Phase 10), DEV-005 (sourcing/pricing re-run flagged), DEV-006 (replay vs live evaluation).

### Migrations

`d9a4c1e7b209` revises `c3e8a1b74f20`. Upgrade from an empty database passes (`tests/test_cli_and_schema.py::test_upgrade_from_empty_database`). Downgrade → upgrade was run successfully. `alembic check` reports only the pre-existing `opportunities_fts` expression-normalization diff from Phase 0.

### Tests

Local PostgreSQL 16 + pgvector on `localhost:5432` (installed on the VM; same image family as CI).

- Dependency check before coding: `python3 -m pytest` → 147 passed, 1 skipped (Phases 0, 1, 7, 8 suites all green).
- `python3 -m pytest tests/test_compliance.py` → 25 passed (DB-backed; passes a second time against a populated database).
- `python3 -m pytest` → 172 passed, 1 skipped (SAM live pull; no `SAM_API_KEY`).
- GitHub Actions `pytest` on PR #12: pass (both push and pull_request runs).
- `govcon compliance benchmark` → gate PASS. Aggregate: mandatory recall 1.0, critical recall 1.0, AI citation accuracy 0.9688 (one fixture citation is deliberately paraphrased), canonical citation accuracy 1.0, false-satisfied rate 0.0, amendment-change detection 1.0, conflict recall 1.0, submission-file completeness 1.0, expected-status accuracy 1.0.

### Acceptance criteria (§15.22)

1. Source attribution — every requirement stores file/page/section/quote/snapshot/pass/confidence; no location → `needs_review` (`test_pipeline_requirements_are_source_backed`, inventory test).
2. Independent passes run and reconcile — A/B distinct prompts + contexts, runs persisted (`test_independent_passes_reconcile_and_single_pass_is_kept`).
3. Single-pass never discarded — page limit found only by B is kept and flagged; reconciler unit test.
4. Deterministic validators exist — 19 validators, table-driven test.
5. Deterministic failures not overridden silently — AI SATISFIED blocked + finding; override requires explicit acknowledgment.
6. Unknown distinct — delivery without transit and set-aside without facts stay `unknown`.
7. Clause mapping — known clauses link to library; unknown/re-dated/alternate flagged.
8. Conflicts surfaced — Q&A 15 pages vs Section L 10 pages → both `needs_review` + blocking finding.
9. Amendments invalidate — delivery superseded, previously satisfied requirement stale, proposal section stale, alert, JEV re-run.
10. Critical redundant validation — one method → `needs_review`; deterministic + AI-with-evidence → `satisfied`.
11. Coverage by category — counts, category breakdown, defined percentages, CLI.
12. Satisfied has evidence/validator output — invariant raises; all satisfied rows carry methods.
13. Red-team findings persisted — AI (confirmed/possible) and rule findings.
14. Proposal coverage — counts checked (2 of 3 references → PARTIAL → `missing` + blocking finding); unsupported mapping → `needs_review`.
15. Pre-flight — full §15.18 checklist; unknowns never green.
16. Blockers prevent `ready_to_submit` — refused; green path succeeds; matrix change after pre-flight re-blocks.
17. Overrides need reason + audit — role, reason, version, deterministic acknowledgment; `audit_events` rows.
18. Benchmark in CI — `test_compliance_benchmark_passes_and_metrics_are_measurable` runs in pytest CI; CLI exits non-zero on failure.
19. Recall and citation accuracy measurable — per-case and aggregate metrics.
20. Critical-recall regression blocks release — disabling the scanner or adding an unmet critical expectation fails the gate/CLI.

### VERIFY outcomes

`PHASE_09_COMPLIANCE.md` has no `⚠️ VERIFY` items. Clause titles/dates were checked on acquisition.gov (FAR Part 52, DFARS Part 252, DLAD Part 52) on 2026-09-26; the current DLAD Part 52 lists only 5452.233-9001.

### Known problems

- No live `DEEPSEEK_API_KEY` / `JEV_API_KEY`: AI steps are mocked; JEV routing ran on the Phase 8 rule fallback.
- The deterministic scanner and topic-based stale marking favor recall and add review noise by design.
- PDF tables are extracted as flattened text (inventory marks `text_only`); OCR is still not attempted.
- `anthropic`/`openai` providers remain stubs, so Pass B escalation to a second model family only works once one is implemented; unavailable escalation is reported as a warning.
- Confidence thresholds are unset (uncalibrated); AI-only validation therefore never auto-accepts.

### Unfinished work

None for Phase 9 scope. The compliance UI (DEV-003) and review reopen (DEV-004) are handed off to Phases 14 and 10.

### Recommended next task

**Phase 10 — Collaborative review, AI comment validation, and approval (`PHASE_10_COLLABORATIVE_REVIEW.md`).** Start only after this draft PR passes the Spec/QA gate and merges. Phase 10 should consume `review_reopen_required` findings and the compliance matrix/coverage counts in the review workspace.

- Follow-up (2026-09-26 22:25 UTC, `claude-opus-5-5`): resume check after a reported interruption. Branch head matched the remote and draft PR #12. Re-ran on local PostgreSQL: `pytest` → 172 passed, 1 skipped; `tests/test_compliance.py` → 25 passed; `govcon compliance benchmark` → gate PASS. No code changes were needed.

## 2026-09-26 21:35 UTC — PHASE_08_JEV_DECISION_PACKAGE

- Agent/model identity: Cursor cloud, model `gpt-5.3-codex` (reasoning=high)
- Datetime (UTC): 2026-09-26 21:35 UTC
- Phase/task: PHASE_08_JEV_DECISION_PACKAGE
- Branch: `cursor/phase-08-jev-decision-package-6495`
- Base: `main` at `d0ee176` (Phase 7 squash merge target)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/11
- Head SHA (gate-unblock rerun): `59242f4ae45776f1c78d10a970546c007879878e`

### Files changed

- `src/govcon/decision/provider.py`
- `src/govcon/decision/schemas.py`
- `src/govcon/decision/bundles.py`
- `src/govcon/decision/rules.py`
- `src/govcon/decision/engine.py`
- `src/govcon/decision/providers/jev.py`
- `src/govcon/decision/providers/rule_fallback.py`
- `src/govcon/decision/providers/llm_fallback.py`
- `src/govcon/decision/providers/__init__.py`
- `src/govcon/decision/__init__.py`
- `src/govcon/cli.py` (new `decision` command group)
- `src/govcon/prompts/jev/*.yaml` (all 13 Phase 8 spec contracts materialized)
- `tests/test_decision_engine.py`
- `tests/fixtures/decisions/*.json` (13 calibration cases)
- `.env.example`
- `IMPLEMENTATION_STATUS.md`
- `SPEC_DEVIATIONS.md`
- `DECISIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- DecisionProvider contract and structured provider result envelope.
- `JevDecisionProvider` using JEV-style decision contract (`model/state/questions`) with auth and typed-answer parsing.
- Deterministic `RuleDecisionProvider` heuristics for all 13 bundles.
- Optional `LLMDecisionProvider` fallback for partial JSON patch refinement.
- Hard-rule engine with deterministic override applied before consequential recommendations.
- Bundle metadata/spec hashing and runtime linkage to versioned YAML decision specs.
- Decision engine that:
  - builds structured source-backed state,
  - runs bundle decisions with fallback chain,
  - validates output with Pydantic schemas,
  - persists immutable `decision_runs` history (with supersedes links).
- Preliminary recommendation + review-ready AI decision package persisted to `bid_decisions` and `ai_analyses`.
- Multiple runs per opportunity supported (no overwrite).
- Guardrails preserved: AI cannot directly set `bid_approved` or `submitted`.
- CLI commands:
  - `govcon decision run-bundle`
  - `govcon decision run-package`
  - `govcon decision runs`

### ADRs / DECISIONS touched

- ADR-027: JEV interface verification and endpoint contract.
- ADR-028: hard-rules-first orchestration and provider fallback chain.
- ADR-029: decision package persistence model and human-authority guardrails.

### Migrations

None. `decision_runs` already exists from Phase 0 schema (ADR-002), so Phase 8 reuses it.

### Tests

- ✅ `python3 -m pytest tests/test_decision_engine.py::test_decision_fixture_contract_and_boundaries tests/test_http_prompts.py -q`
- ✅ `python3 -m compileall src/govcon/decision src/govcon/cli.py`
- ✅ Gate-unblock rerun with local PostgreSQL on `localhost:5432`:
  - `python3 -m pytest tests/test_decision_engine.py` → **9 passed, 0 skipped, 0 failed**
  - `python3 -m pytest` → **147 passed, 1 skipped, 0 failed**
  - DB-backed Phase 8 acceptance tests in `tests/test_decision_engine.py` executed (not skipped).
- ✅ HOLD-clearing verification rerun (2026-09-26 21:12 UTC):
  - `DATABASE_URL=postgresql+psycopg://govcon:govcon@localhost:5432/govcon python3 -m govcon.cli db upgrade head` (exit 0)
  - `python3 -m pytest tests/test_cli_and_schema.py::test_upgrade_from_empty_database -q` → **1 passed**
  - `python3 -m pytest tests/test_decision_engine.py -q` (exit 0; DB-backed AC suite executed)
  - `python3 -m pytest -q` (exit 0)

### VERIFY outcomes

Live probes on 2026-09-26:

- `POST https://api.typesafe.ai/v1/systemone` → auth-required response (`403 Must supply an API key`).
- `POST https://thejevai.com/v1/systemone` → auth-required response (`401 sign in`).
- `POST https://www.jevai.org/api/v1/decisions` → auth-required response (`401`).
- `POST .../v1/chat/completions` on JEV hosts → `404` (confirms non-chat contract).
- No `JEV_API_KEY` / `JEV_BASE_URL` were present in this run.

### Known problems

- Full authenticated JEV execution is unverified in this environment due missing key.
- Live JEV behavior remains mocked/fallback-driven in tests because `JEV_API_KEY` is unset in this environment.
- LLM fallback behavior remains configuration-dependent and is intentionally conservative.

### Unfinished work

- None for Phase 8 scope.
- Phase 9+ work remains out of scope for this run.

### Recommended next task

Checkpoint B after this Phase 8 branch merges and gates pass; then proceed to **Phase 9 only**.

## 2026-09-26 20:45 UTC — PHASE_07_ATTACHMENTS_AI_ANALYSIS

- Agent/model identity: Cursor cloud, model `claude-opus-4-6` (effort=high, thinking=true)
- Datetime (UTC): 2026-09-26 20:45 UTC
- Phase/task: PHASE_07_ATTACHMENTS_AI_ANALYSIS
- Branch: `cursor/phase-07-attachments-ai-analysis-83b9`
- Base: `main` at `f711c39` (Phase 6 SAM vendor profiles, competitors, and contact search, pull request #9)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/10

### Files changed

- `src/govcon/ai/schemas.py` — Pydantic models for solicitation_analysis.v1 output validation
- `src/govcon/ai/providers/__init__.py` — AI provider factory (DeepSeek)
- `src/govcon/ai/providers/deepseek.py` — DeepSeek chat completions provider
- `src/govcon/enrich/attachments.py` — Attachment download, SHA-256, dedup, text extraction
- `src/govcon/enrich/extract.py` — PDF/DOCX/XLSX/text extraction
- `src/govcon/enrich/summarize.py` — Structured solicitation analysis orchestrator
- `src/govcon/enrich/__init__.py` — Package docstring
- `src/govcon/prompting/registry.py` — Prompt registry sync, activation, and load
- `src/govcon/prompting/renderer.py` — Compose system prompt from shared fragments + task prompt
- `src/govcon/prompts/shared/source_security_rules_v1.md` — Production body (§38.1)
- `src/govcon/prompts/shared/no_fabrication_rules_v1.md` — Production body (§38.2)
- `src/govcon/prompts/shared/evidence_rules_v1.md` — Production body (§38.3)
- `src/govcon/prompts/shared/company_facts_policy_v1.md` — Production body (§38.4)
- `src/govcon/prompts/deepseek/solicitation_analysis_v1.md` — Production body (§39.1)
- `src/govcon/cli.py` — CLI commands: enrich download/analyze/process/ingest-file, prompts sync/activate
- `pyproject.toml` — Added pypdf, python-docx, openpyxl dependencies
- `tests/test_attachments_analysis.py` — 34 Phase 7 tests
- `tests/test_http_prompts.py` — Updated placeholder test for Phase 7 activations
- `tests/fixtures/solicitation_fixture.pdf` — DLA solicitation fixture for acceptance testing
- `.env.example` — Updated DeepSeek documentation
- `IMPLEMENTATION_STATUS.md` — Phase 7 complete
- `DECISIONS.md` — ADR-023 through ADR-026
- `SPEC_DEVIATIONS.md` — Phase 7 entry
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- **Attachment ingestion**: `download_attachments` for pursued/reviewing opportunities. Downloads source attachments from `links.resourceLinks`, `links.attachments`, and `raw.resourceLinks`. Computes SHA-256. Preserves original files under `DATA_DIR/attachments/<opp_id>/`. Extracts text from PDF (pypdf), DOCX (python-docx), XLSX (openpyxl), and plain text. Tracks extraction status (`success`/`partial`/`unsupported`/`error`) and errors. Deduplicates on `(opportunity_id, url, sha256)`. OCR is optional fallback, not default.
- **DeepSeek AI provider**: OpenAI-compatible chat completions at `https://api.deepseek.com/chat/completions`. Models: `deepseek-flash`, `deepseek-v4-pro`. JSON output mode. Goes through `authorize_external_call` gateway. Graceful `NoProviderConfigured` when no API key is set.
- **Prompt registry**: `sync_prompts` scans source-controlled prompt files and upserts to `prompt_registry`. `activate_version` sets exactly one active version per prompt. `load_prompt` / `load_prompt_from_disk` resolve prompts by name and version.
- **Shared prompt fragments**: `source_security_rules_v1`, `no_fabrication_rules_v1`, `evidence_rules_v1`, `company_facts_policy_v1` populated with production text from §38.
- **Prompt renderer**: Resolves `includes` front-matter to compose system prompts from shared fragments + task prompt.
- **Solicitation analysis**: `solicitation_analysis_v1` prompt (§39.1) with structured output schema (`SolicitationAnalysisV1` Pydantic model). Validated JSON with source refs. Persisted to `ai_analyses` with full prompt and context metadata. Malformed output fails closed.
- **CLI**: `govcon enrich download`, `govcon enrich analyze`, `govcon enrich process`, `govcon enrich ingest-file`, `govcon prompts sync`, `govcon prompts activate`.

### ADRs / DECISIONS touched

- ADR-023: DeepSeek API contract verified, provider layer design
- ADR-024: Prompt registry, shared fragments, no-hardcoded-prompt rule
- ADR-025: Attachment ingestion design (download, SHA-256, extraction, dedup)
- ADR-026: Structured solicitation analysis output and persistence
- Phase 0–6 ADRs were not changed.

### Migrations

None. Phase 7 uses the Phase 0 `files`, `ai_analyses`, and `prompt_registry` tables without schema changes. Upgrade from an empty database works.

### Tests

Command: `pytest`

Result: **138 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- Fixture PDF (`tests/fixtures/solicitation_fixture.pdf`) extracts text with solicitation number, NSN, quantity, and all section content.
- Mock AI provider produces valid structured JSON conforming to `solicitation_analysis.v1`.
- Key values (NSN, quantity, delivery, submission) map to source references with page and section.
- No `DEEPSEEK_API_KEY` produces an `AnalysisWarning`, not a crash.
- Analysis output is saved to `ai_analyses.output_json`, not overwriting opportunity source fields.
- Prompt metadata (name, version, hash, schema version, generation settings, context manifest) is recorded.
- Dedup: same file downloaded twice stores only one row.
- SHA-256 matches the computed hash of the fixture PDF bytes.
- DOCX and XLSX extraction produce text from their content.
- Prompt registry sync writes active prompts to `prompt_registry`.
- No hardcoded prompts in business-service modules (AST scan).
- Malformed JSON response returns None (fails closed).
- Re-running analysis without force returns the existing row.
- No extracted files returns None (no analysis attempted).

### VERIFY outcomes

Checked live on 2026-09-26 against https://api-docs.deepseek.com/:

- Endpoint: `POST https://api.deepseek.com/chat/completions` (OpenAI-compatible format)
- Authentication: `Authorization: Bearer <key>` header
- Models: `deepseek-flash` (DeepSeek-V4.1-Flash), `deepseek-v4-pro` (DeepSeek-V4-Pro-0813)
- Legacy IDs `deepseek-chat` and `deepseek-reasoner` are retired (redirected to V4.1-Flash)
- JSON output: `response_format: {"type": "json_object"}` supported
- Context length: 1M tokens for both models
- Usage: `prompt_tokens`, `completion_tokens`, `total_tokens` in response
- No `DEEPSEEK_API_KEY` was available in this run. The provider was tested with mocks.

### Known problems

- No live `DEEPSEEK_API_KEY` was available. AI analysis was tested with mock responses only.
- Image-only PDFs (scanned documents) get `partial` extraction status; OCR is not attempted.
- The front-matter parser uses simplified `key: value` parsing, not full YAML. Includes must be comma-separated.
- Attachment download for SAM opportunities requires the SAM API key for `resourceLinks` URLs.
- Token/cost tracking records usage from the provider response but does not compute dollar cost.

### Unfinished work

- Phase 8 JEV preliminary decision engine and AI decision package — not started.
- OCR fallback for image-only PDFs — optional, not default per spec.
- OpenAI and Anthropic provider implementations — later phases.
- Prompt regression suite and evaluation metrics — later phases (§43.6, §43.7).
- Live AI analysis with a real `DEEPSEEK_API_KEY` — not executed here.
- MCP, semantic search, web UI, bid submission — later phases.

### Recommended next task

**Phase 8** — JEV preliminary decision engine + AI decision package (`PHASE_08_JEV_DECISION.md`). **Do not start until this Phase 7 pull request merges and CI gates pass.**

## 2026-09-26 20:35 UTC — PHASE_06_VENDORS_COMPETITORS

- Agent/model identity: Cursor cloud, model `composer-2.5` (fast=true)
- Datetime (UTC): 2026-09-26 20:35 UTC
- Phase/task: PHASE_06_VENDORS_COMPETITORS
- Run: https://cursor.com/agents/bc-6fe4f410-96c7-51c7-8cdd-3d627e17619f
- Branch: `cursor/phase-06-vendors-competitors-619f`
- Base: `main` at `8825ea0` (Phase 5 USAspending awards and pricing, pull request #8)
- Pull request: pending

### Files changed

- `src/govcon/ingest/sam_entities.py`
- `src/govcon/intelligence/vendors.py`
- `src/govcon/intelligence/competitors.py`
- `src/govcon/intelligence/contacts.py`
- `src/govcon/intelligence/__init__.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/config.py`
- `tests/test_vendors.py`
- `tests/fixtures/sam_entity_v3.json`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- Lazy SAM entity lookup via `ensure_vendor` / `vendor_profile`. Maps registration status, CAGE/UEI, business types, NAICS, PSC, and points of contact into the Phase 0 `vendors` table.
- Cache keyed on `fetched_at` and `SAM_VENDOR_CACHE_HOURS` (default 24). `--refresh` bypasses a fresh cache row.
- Vendor profile adds computed award stats from stored USAspending rows: award count, total obligations, top agencies, top PSCs.
- `competitor_summary` returns top winners for the same NSN, PSC, agency path segments, and office on an opportunity.
- `search_contacts` finds harvested buyer contacts by name, agency path, or email substring.
- CLI: `govcon vendors show`, `govcon vendors competitors`, `govcon contacts search`.
- Buyer contact harvesting from opportunities remains the Phase 1 upsert path; Phase 6 adds search only.

### ADRs / DECISIONS touched

- ADR-022 in `DECISIONS.md`: SAM entity v3 contract, cache window, award-stat computation, agency-segment and office competitor matching, contact search.
- `SPEC_DEVIATIONS.md` Phase 6: no behavior deviation from `MASTER_SPEC_v2.5.md`.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, Phase 2 ADR-018, Phase 3 ADR-019, Phase 4 ADR-020, and Phase 5 ADR-021 were not changed.
- No new table and no Alembic revision.

### Migrations

None. Phase 6 uses the Phase 0 `vendors` and `contacts` tables and Phase 5 `awards` rows.

### Tests

Command: `pytest`

Result: **104 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- Vendor lookup returns SAM registration fields plus computed award count, obligations, top agencies, and top PSCs.
- A cached vendor within `SAM_VENDOR_CACHE_HOURS` does not trigger a second SAM HTTP call; `--refresh` does.
- Competitor summary includes NSN, PSC, agency, and office buckets when matching awards exist.
- Contact search matches harvested contacts by name, agency path, and email.
- CLI `vendors show`, `vendors competitors`, and `contacts search` print the stored facts.

### VERIFY outcomes

Checked on 2026-09-26 against https://open.gsa.gov/api/entity-api/

- Endpoint: production `GET https://api.sam.gov/entity-information/v3/entities` (v4 also documented; v3 used for stability with published examples).
- Authentication: required `api_key` query parameter, same key family as Get Opportunities.
- Lookup parameter: `ueiSAM` accepts one 12-character UEI.
- Sections requested: `entityRegistration`, `coreData`, `assertions`, `pointsOfContact`. NAICS and PSC come from `assertions.naicsList` and `assertions.pscList`.
- Response envelope: `entityData` array. Public tier exposes registration, address, business types, NAICS, PSC, and POC names; FOUO email/phone require a higher-tier key.
- Rate limits: 10 / 1,000 / 10,000 requests per day by account type (documented on the entity API page). HTTP 429 is retried through `govcon.http.request_with_retry`.
- Fixture: `tests/fixtures/sam_entity_v3.json` follows the documented response shape for UEI `ZJEUBM5FYLQ2` (Cardinal Health from the Phase 5 USAspending fixture).
- No live `SAM_API_KEY` was available in this run. Network verification used the official documentation and mocked HTTP in tests.

### Known problems

- Public API keys may omit FOUO contact email and phone even when POC names are present.
- Agency competitor matching tries dot-separated `agency_path` segments against `awarding_agency`; mismatched naming between SAM notices and USAspending labels can leave the agency bucket empty.
- Office matching uses SAM `office` or the last `agency_path` segment against `Awarding Sub Agency` and `awarding_agency`; DIBBS rows have no office field.
- Opted-out entities return placeholder strings; those fields are stored as null rather than the placeholder text.

### Unfinished work

- Phase 7 attachments and structured solicitation analysis (`PHASE_07_ATTACHMENTS_AI_ANALYSIS.md`) — not started in this run.
- MCP `vendor_profile` / `competitor_summary` tools, semantic search, web UI opportunity pages, and digest competitor enrichment stay in later phases.
- Live SAM entity pull after `SAM_API_KEY` is set was not executed here.

### Recommended next task

**Checkpoint A** — after this Phase 6 pull request merges and CI gates pass, verify that live federal data, search, historical awards, and vendor intelligence are genuinely useful before investing in Phase 7+. Do not start Phase 7 until Checkpoint A is complete and the user approves further investment.

## 2026-09-26 20:20 UTC — PHASE_05_AWARDS_PRICING

- Agent/model identity: Cursor cloud, model `grok-4.7` (reasoning_effort=high, fast=true)
- Datetime (UTC): 2026-09-26 20:20 UTC
- Phase/task: PHASE_05_AWARDS_PRICING
- Run: https://cursor.com/agents/bc-aece79cb-a5c1-56a3-965a-7e385d39ff80
- Branch: `cursor/phase-05-awards-pricing-ff80`
- Base: `main` at `7577c61` (Phase 4 DIBBS ingestion, pull request #7)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/8
- Implementation commit: `9043831`

### Files changed

- `src/govcon/ingest/usaspending.py`
- `src/govcon/ingest/runs.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/matching/pricing.py`
- `src/govcon/intelligence/awards.py`
- `src/govcon/intelligence/__init__.py`
- `src/govcon/alerts/digest.py`
- `src/govcon/cli.py`
- `alembic/versions/c3e8a1b74f20_award_recompete_view.py`
- `tests/test_usaspending.py`
- `tests/fixtures/usaspending_spending_by_award.json`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon ingest usaspending` pulls contract awards (`A`/`B`/`C`/`D`) for PSC and NAICS codes on enabled watchlists. The first successful run uses a 3-year action-date window. Later runs use `last_modified_date` from the day before that window through today.
- `govcon ingest usaspending --backfill` forces the 3-year window. `--from`/`--to` is an explicit window. `--file` ingests a local search document and does not move the watermark.
- Each result is upserted on `(source='usaspending', award_id=generated_internal_id)`. The search object is stored in `raw`. An unchanged payload does not rewrite the row.
- NSN is the first dashed or NSN-labeled 13-digit value in the description. Quantity and unit price are taken only from explicit keys or from a quantity / unit price / unit cost label. Award amount is never divided into a unit price.
- `price_history`, `price_history_psc`, `award_history_for_agency`, and `top_awardees` read stored rows. CLI: `govcon awards price-history`, `price-history-psc`, `history`, `top`, and `recompete`.
- Migration `c3e8a1b74f20` adds view `award_recompete_candidates` for awards whose action date is at least 18 months ago and whose period end is missing or within the next 18 months.
- The Phase 3 digest adds a "Recent award comps" section when stored awards match the opportunity NSN, or the PSC when that NSN has no rows. Unit price is omitted when it is null.

### ADRs / DECISIONS touched

- ADR-021 in `DECISIONS.md`: search contract, separate PSC/NAICS requests, incremental watermark, null unit price, and the recompete view.
- `SPEC_DEVIATIONS.md` Phase 5: no behavior deviation from `MASTER_SPEC_v2.5.md`.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, Phase 2 ADR-018, Phase 3 ADR-019, and Phase 4 ADR-020 were not changed.
- `finish_run` accepts an optional `details` object on `ingestion_runs.errors`. Callers that omit it keep the previous `messages` payload.

### Migrations

- `c3e8a1b74f20_award_recompete_view.py` revises `97cb081e9a8e`.
- Adds function `govcon_award_period_end(jsonb)` and view `award_recompete_candidates`.
- No new awards columns. Upgrade from an empty database is covered by the existing schema test.

### Tests

Command: `pytest`

Result: **95 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- A known NSN returns vendor, date, and amount, including the undashed form of the same NSN.
- Unit price is returned only when the row has one. Quantity 10 and obligation 1000 do not produce a unit price. A labeled unit price of 25 is stored as 25, not as obligation divided by quantity.
- The 2026-09-26 live fixture for PSC 6515 extracts NSN `6545-01-632-0167` and leaves quantity, unit price, and set-aside null.
- A two-page mock pull inserts both awards, and the second pull is unchanged after a database reload.
- After a successful backfill watermark, the next request uses `last_modified_date` and starts one day earlier.
- Enabled PSC and NAICS codes are separate requests. Disabled codes and a run with no codes do not expand the pull.
- A repeated page fails the run.
- PSC keyword matching is whole-word. Agency history and top awardees sum obligations and do not emit a unit price.
- The recompete view includes an older open award and an older award ending soon, and excludes a recent award and an older award with a far period end.
- The digest HTML shows recent comps with unit price only on the row that has one.
- CLI `--file` ingest is idempotent, and `awards price-history` / `top` / `recompete` print the stored facts.

### VERIFY outcomes

Checked live on 2026-09-26 against `https://api.usaspending.gov` and the USAspending API contracts for spending-by-award, search filters, and award detail.

- Endpoint: `POST /api/v2/search/spending_by_award/`. No API key. A sample PSC `R425` search, a page 2 request, a PSC prefix `R4`, a NAICS prefix `5415`, and a keyword `NSN` search all returned HTTP 200.
- Pagination: `page` is 1-based. `page_metadata.hasNext` was true when more rows existed. `limit` 100 succeeded. `limit` 101 returned HTTP 422 with max 100.
- Fields used: `generated_internal_id`, `Award ID`, `Recipient Name`, `Recipient UEI`, `Award Amount`, `Base Obligation Date`, `Start Date`, `End Date`, `Description`, `PSC.code`, `NAICS.code`, `Awarding Agency`, `Awarding Sub Agency`, `Last Modified Date`.
- `date_type` must be set. The filter docs compare an omitted type's start to `action_date` and its end to `date_signed`. This client sends `action_date` or `last_modified_date` for both bounds.
- Award detail `GET /api/v2/awards/CONT_AWD_80MSFC18C0011_8000_-NONE-_-NONE-/` includes `type_set_aside` and does not include quantity or unit price. Bulk ingest stores the search payload and does not call that endpoint per row.
- Fixture: `tests/fixtures/usaspending_spending_by_award.json` is two live PSC `6515` rows captured on 2026-09-26 (`SPE2DM26FVSMW`, `SPE2DM26FVSMV`), both describing NSN `6545-01-632-0167`.
- The endpoint list checked the same day does not publish a numeric quota. HTTP 429 and 5xx still retry through `govcon.http.request_with_retry`.

### Known problems

- Search rows do not carry quantity or unit price, so those columns stay null unless the description or an explicit key states them.
- Set-aside is not on the search field list. It stays null for ordinary search rows.
- A failed pull can commit awards fetched from earlier pages in that attempt. The watermark does not advance, so the next run repeats the window.
- More than 1000 pages fails the run instead of saving a partial silent cutoff.
- The recompete rule is 18 months. The phase text says "older awards" and does not name that interval.

### Unfinished work

- Phase 6 vendors and competitor intelligence (`PHASE_06_VENDORS_COMPETITORS.md`) — not started in this run.
- IDV award types and per-award detail fetches are not part of this pull.
- USAspending scheduling stays in Phase 17.

### Recommended next task

Phase 6 — vendors, contacts, and competitor intelligence (`PHASE_06_VENDORS_COMPETITORS.md`). **Do not start until this Phase 5 pull request is merged and its gates pass.**

- Follow-up (2026-09-26 20:22 UTC): `IMPLEMENTATION_STATUS.md` Phase 5 Commit/PR column updated to https://github.com/rahulreddykarne/GovCon-platform/pull/8.

## 2026-09-26 20:00 UTC — PHASE_04_DIBBS

- Agent/model identity: Cursor cloud, model `grok-4.7` (params: reasoning_effort=high, fast=true)
- Datetime (UTC): 2026-09-26 20:00 UTC
- Phase/task: PHASE_04_DIBBS
- Run: https://cursor.com/agents/bc-2058fbc4-7d59-5a06-a61e-fd49eb18a6d6
- Branch: `cursor/phase-04-dibbs-a6d6`
- Base: `main` at `49a3b99` (Phase 3 alert digests, pull request #6)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/7
- Implementation commit: `4e71f4c`

### Files changed

- `src/govcon/ingest/dibbs.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/config.py`
- `tests/test_dibbs.py`
- `tests/fixtures/dibbs/in260925.txt`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon ingest dibbs` downloads the newest fixed-width DIBBS index (`inYYMMDD.txt`) from the recent RFQ page, retains the original bytes under `DATA_DIR/dibbs/`, and upserts one opportunity per line.
- `govcon ingest dibbs --date YYYY-MM-DD` downloads that post date's index. `govcon ingest dibbs --file` ingests a local index and does not open the network.
- Parsed fields are solicitation number, dashed NSN when the value is 13 digits, nomenclature, quantity, unit, return-by date, set-aside code, buyer code, AMSC, and the RFQ record URL.
- `source_id` is `solicitation:purchase_request`. A changed line writes a snapshot and field events through the Phase 1 upsert. An unchanged re-run does not.
- NSN and quantity coverage are logged. The saved 2026-09-25 file is 521/523 NSNs and 523/523 quantities.
- DIBBS rows are ordinary `source='dibbs'` opportunities. The Phase 2 watchlist engine matches them on NSN, set-aside, keyword, and source without a matcher change.
- `caYYMMDD.zip` and `bqYYMMDD.zip` are not downloaded. Individual RFQ HTML pages are not fetched.

### ADRs / DECISIONS touched

- ADR-020 in `DECISIONS.md`: index layout, line identity, dashed NSN, FSC in `psc_code`, return-by end of UTC day, consent banner, and request spacing.
- DEV-002 in `SPEC_DEVIATIONS.md`: retain the index only.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, Phase 2 ADR-018, and Phase 3 ADR-019 were not changed.

### Migrations

None. Phase 4 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **81 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- The 2026-09-25 fixture (newest index listed on 2026-09-26) ingests 523 rows.
- Coverage is logged: NSN 521/523 (0.9962), quantity 523/523 (1.0000).
- A second ingest inserts 0, updates 0, and leaves the snapshot count unchanged.
- A changed quantity and return-by write `quantity_changed`, `deadline_changed`, and a second snapshot. A later payload with status `cancelled` writes `cancelled`.
- A dibbs-only NSN watchlist matches the DIBBS row and not a SAM row with the same NSN. A sam+dibbs watchlist matches both. Set-aside `Y` matches 106 DIBBS rows. Keyword `helmet` matches the fixture row.
- CLI `--file` prints coverage and is idempotent. Consent download and newest-index selection are mocked and do not request the zip files.

### VERIFY outcomes

Checked live on 2026-09-26.

- Pages: `https://www.dibbs.bsm.dla.mil/` is DIBBS 6.3.2. Downloads point at `https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent`. Documents are on `https://dibbs2.bsm.dla.mil/`.
- Batch mechanism: each post date has `caYYMMDD.zip` (solicitation PDF/HTML), `inYYMMDD.txt` (index), and `bqYYMMDD.zip` (quote template). Layout help is `https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx`. The index is 140 fixed-width characters. The quote template is comma-delimited and documented at the batch-quoting help page (last updated 30-APR-2024).
- Fixture: `tests/fixtures/dibbs/in260925.txt` (74,266 bytes, 523 records) from `https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt`. The recent page at 2026-09-26 19:48 UTC listed 09-25-2026 as the newest post date. Help text says today's file is posted the next day. 2026-09-26 is a Saturday, so no `in260926.txt` was listed.
- Access: public information may be copied (DLA privacy notice). Unauthorized uploads are prohibited. A session consent cookie is set by posting `butAgree=OK`. Account terms apply to quoting users.
- Robots: `https://www.dibbs.bsm.dla.mil/robots.txt` is HTTP 404 after consent. `https://dibbs2.bsm.dla.mil/robots.txt` is the documents site's file-not-found page. No disallow or crawl-delay is published. This client waits 2 seconds between requests by default.
- One record URL was opened to confirm the pattern: `https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn=SPE1C126T1698` returned the RFQ record. That page is not fetched during ingest.

### Known problems

- The index has a buyer code, not a buyer name or email. Contact rows are not created.
- Two of 523 lines are item type `1` but not 13-digit NSNs (`5815LLNC02443`, `9535LLNCA9756`). They keep quantity and leave `nsn` null.
- The index has no cancellation column. A line missing from a later day is not marked cancelled.
- `psc_code` stores the FSC (first four digits of a 13-digit NSN). The index has no separate PSC or NAICS.
- Return-by is stored as 23:59:59 UTC because the file has a date only.
- If the process stops after the file is saved and before the database commit, the next run ingests the retained file again. The database upsert is still idempotent.

### Unfinished work

- Phase 5 USAspending awards and pricing (`PHASE_05_AWARDS_PRICING.md`) — not started in this run.
- PDF and HTML solicitation files inside `caYYMMDD.zip` stay out of this ingest.
- DIBBS scheduling stays in Phase 17.

### Recommended next task

Phase 5 — USAspending awards and pricing (`PHASE_05_AWARDS_PRICING.md`). **Do not start until this Phase 4 pull request is merged and its gates pass.**

## 2026-09-26 19:10 UTC — PHASE_03_ALERTS

- Agent/model identity: Cursor cloud, model `grok-4.7-high-fast`
- Datetime (UTC): 2026-09-26 19:10 UTC
- Phase/task: PHASE_03_ALERTS
- Run: https://cursor.com/agents/bc-24fe0acb-591b-53a4-ab81-71fab101b177
- Branch: `cursor/phase-03-alerts-b177`
- Base: `main` at `91bdc73` (Phase 2 watchlist matching, pull request #5)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/6
- Implementation commit: `f8b567c`

### Files changed

- `src/govcon/alerts/digest.py`
- `src/govcon/alerts/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/config.py`
- `tests/test_alerts.py`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon alerts digest` collects unalerted `new` matches on enabled watchlists and groups them by watchlist in one HTML message.
- The message includes title, agency, source, PSC, NAICS, set-aside, deadline, UTC calendar days remaining, estimated value when stored, and a direct http(s) source link.
- SMTP is used when `SMTP_HOST` and `ALERT_EMAIL_TO` are both set. Otherwise the HTML file is written under `OUTBOX_DIR`.
- An empty run sends no message and writes no file.
- A repeat run does not alert the same match. Delivery sets `alerted_at` and moves `new` to `seen`.
- When `ALERT_ON_MATERIAL_DEADLINE_CHANGE` is true (the default), a `deadline_changed` event newer than `alerted_at` can send one amendment alert. The same event is not sent again. Other field changes do not re-alert. Setting the flag false turns the amendment alert off.
- Historical awards, competitors, and bid recommendation status are not rendered. DIBBS, USAspending, vendors, AI analysis, JEV, MCP, semantic search, and the web UI were not started.

### ADRs / DECISIONS touched

- ADR-019 in `DECISIONS.md`: one digest per run, SMTP versus outbox, `alerted_at` watermark, deadline-only re-alert, no new table.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, and Phase 2 ADR-018 were not changed.

### Migrations

None. Phase 3 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **67 passed, 1 skipped** (Phase 1 live-pull skip unchanged). The same command passed a second time against the database left by the first run.

Covered acceptance checks:

- Empty day sends nothing and writes no outbox file.
- Repeat run does not write a second file and does not move `alerted_at`.
- A material deadline change generates an amendment alert when `ALERT_ON_MATERIAL_DEADLINE_CHANGE` is true, and a third run does not repeat it.
- The same deadline change sends nothing when the flag is false.
- A non-deadline field change does not re-alert.
- HTML includes the required fields, escapes text, and ignores non-http(s) links.
- SMTP delivery is mocked; a failed send leaves the match unalerted and redacts the SMTP password.
- `govcon alerts digest` writes the outbox and a second invocation reports no message.

### VERIFY outcomes

`PHASE_03_ALERTS.md` contains no `⚠️ VERIFY` items. No live external interface was required. Master §2 already names `smtplib`, and the SMTP and `OUTBOX_DIR` settings were added in Phase 0. SMTP behavior was checked with a fake client, not a live mail server.

### Known problems

- If the process dies after SMTP accepts the message, or after the outbox file is written, and before the database commit, the next run can deliver that digest once more.
- A sources-only watchlist still matches every opportunity from its sources, so the digest can be broad until targeting codes are set.
- Days remaining are whole UTC calendar days. The matcher still uses exact 86400-second spans for `min_deadline_days`.
- Digest scheduling is not wired. Phase 17 owns the job clock.

### Unfinished work

- Phase 4 DIBBS ingestion (`PHASE_04_DIBBS.md`) — not started in this run.
- Later digest enrichment (historical awards, likely competitors, bid recommendation status) stays in later phases.
- In-app notification center and bid/proposal notifications were not built.

### Recommended next task

Phase 4 — DIBBS ingestion (`PHASE_04_DIBBS.md`). **Do not start until this Phase 3 pull request is merged and its gates pass.**

## 2026-09-26 18:58 UTC — PHASE_02_MATCHING

- Agent/model identity: Cursor cloud, model `composer-2.5` (CoS-selected)
- Datetime (UTC): 2026-09-26 18:58 UTC
- Phase/task: PHASE_02_MATCHING
- Run: https://cursor.com/agents/bc-a779864f-ed95-592a-a2ab-61aabff2d53f
- Branch: `cursor/phase-02-matching-d53f`
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/5
- Commits: `fef3276`, `90ab931`

### Files changed

- `src/govcon/matching/engine.py`
- `src/govcon/matching/watchlists.py`
- `src/govcon/matching/__init__.py`
- `src/govcon/cli.py`
- `tests/test_matching.py`
- `tests/test_cli_and_schema.py`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- Deterministic watchlist matching: every non-empty rule group must pass; values inside a group are OR'd; empty groups are wildcards.
- Rule groups: PSC prefix, NAICS prefix, keywords, exclude keywords (whole-word veto), exact NSN, set-asides, source filters, min/max estimated value when known, minimum days until deadline.
- Unknown opportunity values do not fail value filters; `matched_on.groups.value.status` is `unknown`.
- Rule-hit score and explainable `matched_on` JSON evidence per match.
- Idempotent upsert on `(opportunity_id, watchlist_id)`; existing match `status` is preserved on update.
- CLI: `govcon match run`, `govcon match rebuild --watchlist N`, `govcon watchlist add`, `govcon watchlist list`, `govcon watchlist edit`, `govcon watchlist disable`.

### ADRs / DECISIONS touched

- ADR-018 in `DECISIONS.md`: matching semantics, whole-word keyword/exclude handling, unknown value behavior, rebuild stale-match removal, idempotent upsert.
- Phase 0 ADR-001 through ADR-014 and Phase 1 ADR-015 through ADR-017 were not changed.
- No new table and no Alembic revision.

### Migrations

None. Phase 2 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **50 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- PSC prefix match and non-match.
- NAICS prefix match and non-match.
- Exclude keyword veto (whole-word; `unclassified` does not trigger `classified`).
- Wildcard empty PSC group while NAICS still filters.
- Unknown estimated value passes when `max_value` is configured; known out-of-range value fails.
- Idempotent match upsert (second run inserts 0, unchanged 1).
- CLI `match run`, `watchlist add/list/disable`, and `match rebuild` stale removal.

### VERIFY outcomes

`PHASE_02_MATCHING.md` contains no `⚠️ VERIFY` items. No live external interface verification was required for this phase.

### Known problems

- A sources-only watchlist (such as the seeded demo watchlist before targeting arrays are filled) matches every opportunity from the configured sources. Operators should add PSC/NAICS/keyword filters before expecting a narrow shortlist.
- `govcon match run` evaluates all enabled watchlists; disable unused watchlists to avoid broad matches.

### Unfinished work

- Phase 3 alert digests (`PHASE_03_ALERTS.md`) — not started in this run.
- Semantic/vector matching, AI bid decisions, and proposal workflow remain out of scope per phase boundaries.

### Recommended next task

Phase 3 — alert digests (`PHASE_03_ALERTS.md`). **Do not start until pull request #5 is merged and CI gates pass.** Phase 3 (alerts), semantic matching, AI bid decisions, and proposal workflow were not started in this run.

- Follow-up (2026-09-26 18:59 UTC): `IMPLEMENTATION_STATUS.md` Phase 2 Commit/PR column updated to https://github.com/rahulreddykarne/GovCon-platform/pull/5.

## 2026-09-26 18:50 UTC — PHASE_01_SAM_INGESTION

- Agent/model identity: Cursor cloud, model `grok-4.7-high-fast`
- Datetime (UTC): 2026-09-26 18:50 UTC
- Phase/task: PHASE_01_SAM_INGESTION
- Run: https://cursor.com/agents/bc-9e68ed03-0d1b-5e9f-899c-3926d53343a0
- Branch: `cursor/phase-01-sam-ingestion-43a0`
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/3
- Commits: `bba0ca2`, `f9ad00d`, `dbce6ce`

### Files changed

- `src/govcon/ingest/sam_opportunities.py`
- `src/govcon/ingest/snapshots.py`
- `src/govcon/ingest/runs.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/http.py`
- `src/govcon/config.py`
- `src/govcon/models.py` (annotation only: `opportunities.poc` may be a JSON object or array)
- `tests/test_sam_ingestion.py`
- `tests/fixtures/sam_opportunities_search.json`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `README.md`
- `.env.example`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon ingest sam` calls the SAM.gov Get Opportunities search. The default posted window is the last 3 UTC days, inclusive of today. The command pages until `totalRecords`, using `offset` as a page index.
- `govcon ingest sam-backfill` splits a longer posted-date range into windows of at most one year.
- `govcon ingest sam-archive-sweep` marks stored SAM rows `archived` when `archive_date` is before today UTC. It does not open a network client and does not write a snapshot.
- Each opportunity is upserted on `(source='sam', source_id=noticeId)`. The raw search object is stored. A snapshot and field-diff events are written only when the canonical content hash changes. Events are inserted before the current row is updated.
- Tracked diffs include deadline, set-aside, status, title, description hash, links/attachments, quantity, and estimated value min/max. A deadline change emits `deadline_changed`.
- Buyer contacts are upserted when both email and agency path are present.
- NSN, quantity, and estimated value are parsed only from explicit labels or explicit value fields. `award.amount` is not copied into estimated value.
- Description and attachment URLs are kept on the raw payload and `links`. Their bytes are not downloaded.

### ADRs / DECISIONS touched

- ADR-015 in `DECISIONS.md`: production search URL, auth, pagination, notice id, attachment fields, and retry behavior.
- ADR-016 in `DECISIONS.md`: one SHA-256 for `raw_hash` and `opportunity_snapshots.content_hash`; archive sweep is local and does not snapshot.
- ADR-017 in `DECISIONS.md`: contact unique key, NSN shape, and the rule that quantity and estimated value stay null unless the source states them.
- `SPEC_DEVIATIONS.md` Phase 1: no behavior deviation from `MASTER_SPEC_v2.5.md`. The missing live key is recorded there as an environment limit, not a product change.
- Phase 0 ADR-001 through ADR-014 were not changed.
- HTTP retries stay in `govcon.http.request_with_retry`. SAM passes a longer wait and more attempts. The default wait used by other callers is unchanged.
- No new table and no Alembic revision.

### Migrations

None. Phase 1 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **37 passed, 1 skipped** (same tree as this handoff; the 18:50 UTC edit changes only this document).

The skipped test is `test_live_pull_yields_at_least_one_record`. It runs only when `SAM_API_KEY` is set. The key was not set.

Covered acceptance checks:

- Unchanged fixture rerun inserts zero duplicate opportunities and zero duplicate snapshots.
- A changed payload creates one new snapshot.
- A deadline change creates `deadline_changed`, and that event points at the new snapshot.
- Raw source JSON is readable from the opportunity and the snapshot after commit.

### VERIFY outcomes

Source checked live on 2026-09-26: https://open.gsa.gov/api/get-opportunities-public-api/

Every `⚠️ VERIFY` item in `PHASE_01_SAM_INGESTION.md`:

- Current endpoint/version: still `GET https://api.sam.gov/opportunities/v2/search`. Alpha is `https://api-alpha.sam.gov/opportunities/v2/search`. The historical endpoint in the phase spec matches production.
- Authentication method: required `api_key` query parameter. Documented errors are "No api_key was supplied" and "An invalid api_key was supplied".
- Pagination behavior: `limit` is records per page, maximum 1000, API default 1. `offset` is documented as the page index starting at 0. The envelope is `totalRecords`, `limit`, `offset`, `opportunitiesData`. This client requests pages 0, 1, 2. A page that repeats at least half of the previous page's notice ids fails the run instead of truncating.
- Parameter names: required `api_key`, `postedFrom`, `postedTo` (`MM/dd/yyyy`, at most one year apart). Also documented: `limit`, `offset`, `ptype`, `solnum`, `noticeid`, `title`, `state`, `zip`, `organizationCode`, `organizationName`, `typeOfSetAside`, `typeOfSetAsideDescription`, `ncode`, `ccode`, `rdlfrom`, `rdlto`. `deptname` and `subtier` are deprecated. `status` is marked coming soon. The ingest sends `api_key`, `postedFrom`, `postedTo`, `limit`, and `offset`.
- Rate limits: the opportunities page says the daily cap depends on federal, non-federal, or general role and does not list HTTP 429. The SAM.gov System Account User Guide gives default daily caps of 10, 1,000, or 10,000 by account type. This client retries 429 and 5xx through `govcon.http.request_with_retry`, then fails the run.
- Attachment fields: `resourceLinks` is the attachment URL list. `description` is a separate download URL, not the body. `links` and `uiLink` are record links. Bytes are not downloaded in Phase 1.
- Notice/amendment identifiers: `noticeId` (query `noticeid`) is the notice id and is stored as `source_id`. `type` is the current type and `baseType` is the original type. The public API returns only the latest version and has no separate amendment id. `source_version` stores `postedDate` when present.

Some third-party guides treat `offset` as a record offset (`offset = page * limit`). This implementation follows the official page-index wording. That behavior is not yet confirmed with an authenticated second page.

### Known problems

- No `SAM_API_KEY` was available. Requests to the production and alpha search URLs with a demo or invalid key returned an empty HTTP 404 from `istio-envoy`, which does not match the documented invalid-key body. A live SAM record was not fetched.
- The committed fixture is the response example published on the official Get Opportunities page (`noticeId` `5b345bbb7127b91a3ad577b203fc6f68`), not a response captured with a project key.
- Page-index behavior is documented, not confirmed with a successful authenticated second page.

### Unfinished work

- Run `pytest tests/test_sam_ingestion.py::test_live_pull_yields_at_least_one_record` after `SAM_API_KEY` is set, and replace or add a fixture from that live response if the published example should not remain the only real payload.
- Confirm with that key whether `offset` advances by page index. If a live second page shows record-offset behavior, adjust pagination and record the deviation.
- Description-body and attachment downloads belong to a later phase (Phase 7), not this ingest.

### Recommended next task

Phase 2 — watchlist matching engine (`PHASE_02_MATCHING.md`), only after pull request #3 is merged and its gates pass. Phase 2 is not started. `IMPLEMENTATION_STATUS.md` still marks Phase 2 as NOT STARTED.

---

## Phase 11 — Post-approval proposal and submission-package generation

**Status:** COMPLETE  
**Date:** 2026-09-26  
**Model:** claude-sonnet-4-6  
**Branch:** cursor/phase-11-proposal-submission-3a2d

### What was implemented

- **`src/govcon/proposals/versions.py`** — Immutable version management: `create_proposal_version`, `latest_proposal_version`, `list_proposal_versions`, `get_sections_for_version`.
- **`src/govcon/proposals/drafting.py`** — AI proposal drafting via `proposal_drafting` prompt. Reads compliance matrix + approved facts; never fabricates.
- **`src/govcon/proposals/ai_review.py`** — Red-team AI review via `proposal_red_team` prompt. Critical findings become blocking `ComplianceFinding` rows. Requirement IDs validated before FK insert (ADR-042).
- **`src/govcon/proposals/service.py`** — Main orchestration: `get_or_create_proposal`, `generate_proposal` (requires `approved_to_bid`), `get_proposal_workspace`, `finalize_proposal` (APPROVE_FOR_SUBMISSION / RETURN_FOR_FIX / CANCEL_BID), `record_submission_confirmation`.
- **`src/govcon/proposals/export.py`** — `export_proposal_docx` (DOCX via python-docx), `export_coverage_xlsx` (XLSX via openpyxl), `export_submission_zip` (ZIP containing DOCX + XLSX + instructions + checklist + manifest).
- **`src/govcon/submissions/service.py`** — `generate_submission_package` extracts submission instructions from compliance matrix key_values; creates/updates `Submission` record.
- **`src/govcon/submissions/checklist.py`** — `generate_final_checklist` (structured checklist with ready/pending/blocked/unknown status per item), `generate_step_by_step_instructions`.
- **`src/govcon/submissions/email_adapter.py`** — `draft_submission_email` produces To, Subject, body draft with `[[REVIEW:...]]` markers. No auto-send.
- **Prompts activated:** `proposal_drafting_v1.md` and `proposal_red_team_v1.md` (both in `prompts/deepseek/`).
- **New schemas:** `ProposalDraftV1`, `ProposalRedTeamV1`, `ProposalRedTeamFinding`, `ProposalDraftSection` in `compliance/schemas.py`.
- **New metric:** `coverage_summary(requirements)` in `compliance/metrics.py` (flat summary without by_category).
- **Migration:** `a1b2c3d4e5f6` — adds `final_approved_by_user_id`, `final_approved_at`, `red_team_analysis_id`, `submission_id`, and `ck_proposals_status` check constraint to `proposals`.
- **CLI commands:** `govcon proposal generate|status|red-team|approve|return|cancel|export` and `govcon submission package|checklist|instructions|email-draft|confirm`.
- **Test updated:** `tests/test_http_prompts.py` — added `activated_phase11` set to reflect both new active prompts.

### Acceptance criteria verification

| Criterion | Verified by | Result |
|---|---|---|
| AC-1: Approval-to-bid starts proposal/package generation | `test_generate_proposal_creates_version_when_approved`, `test_generate_proposal_requires_approved_to_bid` | PASS |
| AC-2: Proposal sections traceable to requirements | `test_proposal_sections_have_requirement_ids`, `test_placeholder_draft_maps_requirement_ids` | PASS |
| AC-3: Submission instructions from solicitation evidence | `test_submission_package_generated_from_requirements`, `test_submission_instructions_from_checklist`, `test_email_draft_generated` | PASS |
| AC-4: Missing mandatory items block `ready` | `test_missing_mandatory_blocks_ready`, `test_all_satisfied_shows_ready`, `test_checklist_blocked_when_mandatory_missing` | PASS |
| AC-5: Export DOCX/XLSX/ZIP | `test_export_proposal_docx`, `test_export_coverage_xlsx`, `test_export_submission_zip` | PASS |
| AC-6: Final approval human-authorized | `test_final_approval_requires_approver_role`, `test_approve_for_submission_sets_final_approved`, `test_return_for_fix_sets_returned_status`, `test_cancel_bid_sets_pursuit_cancelled`, `test_final_approval_is_audited` | PASS |
| AC-7: No arbitrary portal auto-submission | `test_no_auto_submission_in_v1` | PASS |

Additional tests: red-team critical/major findings, immutable versions (`test_every_regeneration_creates_new_version`), coverage run after generation, CLI smoke tests.

### Design decisions
- ADR-040: Final-approval uses `override_reason="human_final_approval_gate"` to satisfy Phase 9's pre-flight check requirement.
- ADR-041: Proposal red-team severity is `critical|major|minor`; compliance severity remains `critical|high|medium|low`.
- ADR-042: AI-returned requirement IDs are validated against the opportunity's actual requirements before FK insert.

### ⚠️ VERIFY items
Phase 11 has no `⚠️ VERIFY` markers. The spec's AI prompts and schemas are derived from §41 of the master spec and directly implemented without external interface verification.

### Known deviations
None. Full implementation of the spec with no silent deferrals.

### Unresolved blockers
None.

### Recommended next phase

`PHASE_12_MCP.md` — MCP server. Do not implement in this run.

---

## Phase 13 — Semantic search & recommendations

**Status:** COMPLETE  
**Date:** 2026-09-27  
**Model:** claude-sonnet-4-6  
**Branch:** cursor/phase-13-semantic-search-c5b0

### What was implemented

- **`src/govcon/enrich/embeddings.py`** — Full implementation: `EmbeddingProvider` protocol, `SentenceTransformerProvider` (lazy-loaded `all-MiniLM-L6-v2`), `get_default_provider()`, `opportunity_text()`, `watchlist_profile_text()`, `_mean_pool()`, `embed_opportunity()`, `run_embedding_job()`, `build_watchlist_profiles()`, `compute_win_profile()` (requires ≥3 wins), `compute_pursued_profile()`.
- **`src/govcon/matching/semantic.py`** — Full implementation: five category constants, `is_eligible_for_pursuit()`, `_vector_search()` (pgvector `<=>` cosine distance), `similar_opportunities()` (vector → heuristic fallback), `semantic_recommendations_for_watchlist()` (excludes rule-matched opps), `win_profile_recommendations()`, `pursued_profile_recommendations()`, `recompete_radar()` (shared PSC/agency with past award), `all_recommendations()` (all five categories).
- **`src/govcon/mcp/operations.py`** — `op_similar_opportunities` replaced: now delegates to `matching.semantic.similar_opportunities`. DEV-007 resolved.
- **`src/govcon/models.py`** — Added `embedding: Vector(384)` and `embedding_updated_at: DateTime` to `Watchlist`.
- **`alembic/versions/f2a3b4c5d6e7_phase13_semantic_search.py`** — Migration: `watchlists.embedding` column, `watchlists.embedding_updated_at` column, HNSW vector index on `opportunities.embedding` (cosine ops, m=16, ef_construction=64), HNSW index on `watchlists.embedding`.
- **`src/govcon/cli.py`** — Added `embed_app` and `semantic_app` sub-typers; commands: `govcon embed run [--all] [--batch-size N]`, `govcon embed watchlists [--watchlist N]`, `govcon semantic similar <opp_id> [--limit N]`, `govcon semantic recommendations [--watchlist N] [--limit N]`.
- **`pyproject.toml`** — Added `sentence-transformers>=3.0` dependency.
- **`tests/test_semantic_search.py`** — 24 tests with `MockEmbeddingProvider` (no real model loaded in CI).

### Acceptance criteria verification

| Criterion | Verified by | Result |
|---|---|---|
| AC-1: Semantic match works with no keyword overlap | `test_semantic_match_no_keyword_overlap` (different PSC, zero token overlap in titles, still found by vector) | PASS |
| AC-2: Search stays interactive at target scale | `test_hnsw_index_exists` (HNSW index confirmed in pg_indexes), `test_vector_search_executes_without_seqscan_error` | PASS |
| AC-3: Ineligible opportunity flagged, not auto-pursued | `test_ineligible_opportunity_flagged_in_results`, `test_is_eligible_for_pursuit` | PASS |

Additional spec tasks verified:
- Task 1 (embed opportunity text): `test_run_embedding_job`, `test_opportunity_text_combines_fields`
- Task 2 (vector index): `test_hnsw_index_exists`, `test_watchlist_embedding_columns_exist`
- Task 3 (watchlist profiles): `test_build_watchlist_profiles`, `test_watchlist_profile_persisted_to_db`, `test_watchlist_profile_text_combines_fields`
- Task 4 (semantic-only recommendations): `test_semantic_recommendations_for_watchlist_no_profile`, `test_semantic_recommendations_excludes_rule_matches`, `test_all_recommendations_returns_categories`
- Task 5 (win-profile ≥3 wins): `test_win_profile_returns_none_when_insufficient`, `test_win_profile_returns_embedding_with_sufficient_wins`, `test_win_profile_needs_min_wins`
- Task 6 (no bypass hard eligibility): `test_ineligible_opportunity_flagged_in_results`, `test_is_eligible_for_pursuit`
- Task 7 (similar_opportunities MCP): `test_mcp_similar_opportunities_uses_vector`, `test_dev_007_resolved_vector_replaces_heuristic`

Five recommendation categories implemented as specified:
- `Rule match` — Phase 2 rule engine (unchanged)
- `Semantic match` — vector search against watchlist profile embedding, keyword-miss opps only
- `Similar to won bids` — mean embedding of ≥3 won opportunities
- `Similar to pursued bids` — mean embedding of actively pursued opportunities
- `Recompete radar` — open opportunities sharing PSC or agency with past awards

### ⚠️ VERIFY items
Phase 13 has no `⚠️ VERIFY` items. The spec references `all-MiniLM-L6-v2` as an example model; the config setting `EMBEDDING_MODEL` is already in `Settings` and can be overridden. No external API is called.

### Known deviations
- DEV-007 resolved: `op_similar_opportunities` now uses pgvector.
- DEV-008: HNSW index in migration uses non-CONCURRENTLY (Alembic runs inside a transaction; `CONCURRENTLY` would fail). Documented in migration comment. Production teams should pre-create with `CONCURRENTLY` before Alembic upgrade.

### Design decisions
- ADR-044: sentence-transformers local, HNSW index parameters, watchlist profile text construction.
- ADR-045: Five categories; ineligible flagged not filtered; `is_eligible_for_pursuit()` gate.
- ADR-046: Win-profile requires ≥3 genuine wins (graceful empty otherwise).

### Test results
- `tests/test_semantic_search.py` — **24 passed**
- Full suite — **237 passed, 1 skipped** (was 213+1-skipped before Phase 13)

### Unresolved blockers
None.

### Recommended next phase

**`PHASE_14_WEB_UI.md`** — Web UI. Do not implement in this run.
