# Feature fixes — October 2, 2026

Target: the current working tree, preserving all pre-existing uncommitted work. Original bytes of each potentially edited application file were saved outside the repo before editing. No P0 was established by the feature audit. Fix order: **F1, F2, F3, F6, F8, F4, F5, F7** (P1 first, then P2).

## Planned changes and caller/feature inventory

Locations below are the pre-fix audit locations. All modified functions retain their existing signatures unless an approved contract change is explicitly recorded here. No database or production configuration change is planned.

| Finding | Location | Smallest intended fix |
|---|---|---|
| F1 P1 | `web/routes/__init__.py:346` | Create/reuse the pursuit under an opportunity lock in the existing Inbox transaction; preserve empty HTTP 200 response. |
| F2 P1 | `web/templates/workspace/review.html:2` | Expose the existing reviewer-assignment POST form in the no-session branch for approvers. |
| F3 P1 | `web/routes/__init__.py:965` | Keep approval, expose generation failure, and offer an authorized, version-checked, serialized retry. Any needed new HTTP contract requires user approval. |
| F6 P1 | `web/templates/workspace/submission.html:42` | Translate browser-local time to an offset-bearing instant before submitting; preserve server acceptance of existing UTC/offset inputs. |
| F8 P1 | `mcp/operations.py:370` | Preserve response keys while reading a defined decision-result field instead of nonexistent ORM status. |
| F4 P2 | `web/routes/__init__.py:609`; `workspace/compliance.html:38` | Query matrix and preflight by type independently and render real preflight fields. |
| F5 P2 | `workspace/submission.html:14` | Supply/render existing checklist and instruction outputs and actual required files/destination. |
| F7 P2 | `workspace/compliance.html:4` | Correct the displayed option to `--opportunity-id`. |

### F1 callers and affected features

- Direct caller: `create_app` registration for `POST /inbox/action` in `web/app.py:92`; form callers in `web/templates/inbox.html` for Seen, Dismiss, Pursue (Review is a detail link). Existing route tests in `TestInboxActions` and read-only/triage gate tests also call this endpoint.
- Handler dependencies: authenticated actor/`review` permission, match lookup, transaction/session scope, audit recording; new pursuit uses existing model and opportunity-lock discipline.
- Affected features: Inbox Seen/Dismiss/Review/Pursue, invalid-action/missing-match/unauthorized/CSRF responses, idempotent repeated pursue; Pipeline and workspace start/detail. Shared record consumers to recheck: MCP match/pursuit listing/updating, semantic pursued profiles, match reconciliation, alerts (new matches), review approval, proposal/submission workflow, outcomes/learning, and opportunity audit/activity. These consumers keep their implementations and contracts.

### F2 callers and affected features

- Direct template caller: `workspace.html` Review include; rendering entry: `GET /workspace/{opp_id}?tab=review` registered to `workspace`. Form target: existing `workspace_assign_reviewer` (`POST /workspace/{opp_id}/assign`), which calls `ensure_review_session`, `assign_reviewer`, and `recalculate_quorum`.
- Affected features: no-session empty state, role-specific reviewer selection, active-user filtering, CSRF, missing/inactive/unauthorized assignee errors; existing-session assignment, comments, reviewer completion, quorum and approval, notifications/audit. Overview and Start Workspace remain read/create-pursuit paths with no new mutation on GET.

### F3 callers and affected features

- Direct caller of `_trigger_proposal_generation`: `workspace_approve` only, registered as `POST /workspace/{opp_id}/approve` in `web/app.py`. Form callers: `workspace/review.html` approval/return/no-bid forms; potential retry form in `workspace/proposal.html` must use an approved contract.
- Dependencies: `finalize_approval`, `generate_proposal`, `generate_submission_package`, provider availability/current settings, nested transaction, audit/logging.
- Affected features: normal Approve to Bid with AI or placeholder fallback, return/no-bid, successful decision redirect, failure rollback retaining human approval, missing proposal/package display, retry authorization/version/source checks, repeat/concurrent retry, reviewed/edited/final-approved/submitted artifacts, Proposal/Submission/Activity tabs, notification/audit readers. CLI/MCP proposal generation/version and final readiness services keep their contracts.

### F6 callers and affected features

- Direct template caller: Submission include in `workspace.html`; `workspace` supplies context. Manual confirmation form posts to `workspace_submission_approve` registered in `web/app.py:110`, then `record_submission_confirmation`. CLI `submission confirm` calls the same service directly.
- Affected features: optional local time, blank field/server-current-time behavior, explicitly UTC/offset-bearing client inputs, deadline/future-time refusal, proof/version/permission/CSRF validation, recorded submission history, outcome availability; existing final-approved/ready package gates and idempotent confirmations.

### F8 callers and affected features

- Production direct caller: `_tool(ops.op_get_bid_analysis)` in `mcp/server.py:71`, exposed as `get_bid_analysis`; direct operation call in `test_mcp.py:258` and protocol clients.
- Dependencies: opportunity lookup, latest bid decision/package, `list_decision_runs`, compact JSON serialization and structured errors.
- Affected features: bid decision/package reads with no history or populated history; no-analysis/missing-opportunity errors, latest-five history ordering, existing response keys/value serialization, MCP read-only access. Decision CLI/engine and web AI Decision tab remain producers/parallel readers with unchanged contracts.

### F4 callers and affected features

- `workspace` registered at `GET /workspace/{opp_id}` renders `workspace.html`, which includes all 13 tabs: Overview, AI Decision, Requirements, Market, Awards, Products, Pricing, Competitors, Compliance, Review, Proposal, Submission, Activity. Only Requirements/Compliance context is intended to change.
- Compliance include also includes `workspace/requirements.html`; dependencies: requirement query, `ComplianceRun` query/types, matrix counters, preflight `ready/items` and source-revision metadata.
- Affected features: no-run state, matrix counters, later nonmatrix runs, preflight pass/fail/stale reporting, Requirements table; every other workspace tab's context/rendering, missing-opportunity and authentication behavior. CLI/MCP matrix/coverage/preflight readers and underlying validators remain unchanged.

### F5 callers and affected features

- Same `workspace` registration and Submission template include; new template context comes from existing `generate_final_checklist` and `generate_step_by_step_instructions` (also called by submission CLI commands and exports). Their implementations/signatures are not planned for modification.
- Affected features: preparing/ready/submitted Submission tab, actual file names, method/destination/recipient/deadline/timezone, checklist statuses/details, unknown instructions, manual confirmation and structured outcome forms, escaping unsafe text. Proposal/submission CLI output, email drafts, DOCX/PDF/XLSX/ZIP exports and MCP submission status use the same existing services and must still pass.

### F7 callers and affected features

- Direct caller: workspace Compliance include, rendered by `workspace`; indirect include from all workspace navigation using that tab.
- Displayed command consumer: Typer `compliance_run` (`cli.py:2048`, required `--opportunity-id`). Affected feature: no-compliance-run recovery instructions; normal compliance/matrix/requirements/preflight display remains covered by F4 tests.

## Execution log

Baseline, preservation tests, failing regressions, per-fix full checks, caller rechecks, and final feature statuses will be appended as each finding completes. Raw logs/XML/JSON live under `docs/audit/feature_fixes_2026-10-02/`. Tests use disposable PostgreSQL and fixture/mocked integrations only.

| Step | Preservation before implementation | Reproduced failure | Focused green | Full suite / build |
|---|---|---|---|---|
| Baseline | Original tree | — | — | 1,030 passed, 1 skipped; build passed |
| F1 | 13 passed (new cases plus Inbox and refusal tests) | No pursuit after two Pursue requests; regression failed | 13 passed | 1,039 passed, 1 skipped; build passed; zero baseline regressions |
| F2 | 3 passed: role restrictions, GET without writes, existing assignment/review controls and repeat assignment | Empty Review lacked assignment form | 4 passed | 1,043 passed, 1 skipped; build passed; zero baseline regressions |
| F3 | 6 passed: existing complete web workflow plus savepoint rollback retaining approval | Failure still redirects with success only | Pending required approval | Application code unchanged; regression is strict xfail while blocked by contract approval |
| F6 | 4 passed: blank/server-now, explicit UTC, explicit offset, legacy naive UTC | Rendered browser handler sent naive local values in both Chicago seasons | 6 passed | 1,050 passed, 1 skipped + 1 expected F3 failure; build passed; zero baseline regressions |
| F8 | 7 passed: in-process MCP with no history/missing analysis, existing MCP suite | Actual protocol returned operation_failed/AttributeError for real rules decision runs | 8 passed | 1,053 passed, 1 skipped + 1 expected F3 failure; build passed; zero baseline regressions |
| F4 | 1 passed: real matrix/requirements and empty-state counts | Later inventory/preflight displayed zero; actual ready result had no PASS display | 3 passed | 1,057 passed, 1 skipped + 3 expected failures (F3 and next F5 tests); build passed; zero baseline regressions |
| F5 | 2 passed: real confirmation/outcome forms and unapproved gate | No instructions/files/destination; portal guide also absent | 4 passed | 1,060 passed, 1 skipped + 2 expected failures (F3 and next F7 test); build passed; zero baseline regressions |
| F7 | 1 passed: actual CLI parser accepts existing options | Rendered command fails with NoSuchOption: --opp-id | 5 passed including F4 guards | 1,064 passed, 1 skipped + 1 expected F3 failure; build passed; zero baseline regressions |

Typecheck and lint attempts at baseline and every full check report the same missing `mypy` and `ruff` modules. These are NOT verified checks, not passes.

Caller rechecks completed so far:

- **F1:** reread route registration and all Inbox action forms, Pipeline inclusion rules, Start Workspace creation, MCP match/pursuit serializers and gated stage updates, semantic match exclusion, digest filters, approval stage mapping, and audit consumers. Existing field types/statuses and empty-200/error responses are retained. The new pursuit uses the same evaluating stage and audit payload as Start Workspace. Existing pursuits, including submitted ones, are left intact. Shared matching/alert/review/proposal/outcome tests still pass; no live alert was sent.
- **F2:** reread `workspace` context/13 tab includes, assignment POST's active-user/role guards and ensure/assign/quorum calls, decided-session assignment guard, comments/completion/approval forms and notifications. New first assignment uses the existing POST and CSRF field. Reviewer/read-only views remain without the form; GET does not create a session. Existing reviewer completion, approval, notification, and all-tab tests pass.
- **F6:** reread rendered confirmation form, route parsing/blank/invalid-time branches, confirmation service's authorization/version/proof/deadline/source/package/idempotency gates, CLI confirmation, and outcome form. Changed only the browser handler; server signatures, field names, and accepted UTC/offset/legacy inputs are untouched. Real temporary-database confirmation verifies both corrected UTC instants. The JavaScript handler is executed in Node with America/Chicago, not a full browser automation session.
- **F8:** reread sole production caller (registered MCP wrapper), serializer/error envelope, latest bid/package lookups, list_decision_runs ordering/limit, real engine persistence and schemas, plus CLI/web decision consumers. Same response keys; `status` is the actual result's status when present, null for bundle schemas without one. No inferred outcome is invented. Protocol tests verify seven persisted runs become the latest five, repeats do not write, and analysis without history keeps its prior response.
- **F4:** reread `workspace` route and all 13 template includes, requirements query, matrix record/latest-run contract and pipeline/CLI writers, preflight ready/items schema, and readiness blockers' requirement/source/package/instruction freshness checks. Matrix and preflight now have independent run-type selection; saved pass is displayed only while the current gate has no blockers. No-run and normal matrix counts remain correct; requirements and all other tabs continue rendering. No compliance producer or authorization gate was changed.
- **F5:** reread `workspace` submission/proposal fallback queries and includes, unchanged checklist/instruction service outputs, both submission CLI commands, ZIP export caller, email/portal generation, manual confirmation and outcome forms. New context is limited to Submission. The template renders actual checklist details, file names, method/destination, actions and ordered steps with escaping; final approval and confirmation field names/versions/CSRF remain intact. Service, CLI, export, and readiness tests pass. All 13 tabs were additionally run with genuine fixture-generated approved proposal/ready submission state and unchanged versions/statuses/audit count after GETs.
- **F7:** reread empty compliance branch, matrix/preflight branches, route and workspace include, and Typer compliance-run declaration. A test extracts the exact displayed command from an HTTP-rendered page and passes its arguments to the real Typer command parser without executing ingestion or AI work. Only the displayed option changed.

### F3 contract change requiring approval

The failure regression is reproduced: the existing approval POST redirects with only a success notice after a synthetic generation outage. Existing successful-generation tests and the new preservation test confirm that a failed generation must keep the recorded human approval and roll back half-created artifacts.

Proposed exact scope: add `POST /workspace/{opp_id}/proposal/retry` with existing CSRF protection, approver/owner authorization, and `expected_version` from the review session. Under the opportunity lock, require a current approved review and current decision package, reject stale forms, and refuse to regenerate any existing proposal or submission. Reuse the existing savepoint-based generation helper without changing its signature. Record failed/successful attempts in the existing audit table; show a persistent failure message and retry form on the Proposal tab. The original approval POST keeps its 303 response and successful notice, but uses the existing error query parameter when generation fails, while retaining approval. No schema or configuration change.

New callers: the Proposal retry form and its route registration. Rechecked consumers will include approval controls, Proposal/Submission/Activity tabs, reviewer/read-only permissions, CSRF, stale/source-change gates, repeated retries, existing edited/final-approved/submitted artifacts, and the unchanged CLI/MCP generation services.

**Approval requested but not received. F3 implementation is therefore NEEDS INPUT.** No retry route, failure-response change, helper change, or proposal-template change has been made. Its strict expected-failure test records the remaining defect and will fail as an unexpected pass if this behavior later changes without updating the test.

## Touched-feature and caller audit

Evidence: executed synthetic HTTP requests through the actual FastAPI application with CSRF; real migrated disposable PostgreSQL; real DOCX/XLSX package lifecycle; rendered datetime handler executed in Node; registered MCP tool through an in-process FastMCP client; real Typer parser; existing service/integration tests. No government submission, email send, live model API, payment, or production data mutation.

| Feature / caller | Before | After | Evidence / scope |
|---|---|---|---|
| Inbox Pursue; Pipeline inclusion | BROKEN / PARTIAL | WORKS | Creates one evaluating pursuit, visible in Pipeline; repeats and simultaneous requests produce one pursuit/audit creation |
| Inbox Seen, Dismiss, Reviewing; refusal responses | WORKS | WORKS | Characterization tests retain status/audit/empty-200 behavior; invalid/missing/read-only requests retain refusal/no writes |
| Existing pursuit stages and metadata | WORKS | WORKS | Pursue does not reset evaluating/submitted stages, notes, identity or version |
| Start Workspace → first review assignment | BROKEN | WORKS | Start twice creates one pursuit; rendered form creates session and first assignment; inactive assignee rejected without creating a session |
| Existing-session assignments, comments, completion, quorum, approval | WORKS | WORKS | Existing web/collaboration/workflow tests; assignment repeat preserves one row/version; all prior controls rendered |
| Login/roles/CSRF, notifications and audit activity | WORKS | WORKS | Existing auth/security/workflow tests; same registered handlers and CSRF fields; all-tab GET test retains audit count |
| Normal bid approval → proposal/package generation | WORKS | WORKS | Existing complete web lifecycle and service tests; failed package still rolls back half-created artifacts and retains approval |
| Generation-failure reporting/retry | BROKEN | BROKEN — unchanged | Reproduced missing error/success-only redirect; F3 requires the requested contract approval |
| Requirements table and Compliance matrix | PARTIAL | WORKS | Empty and populated states; exact matrix counts survive later inventory and preflight runs |
| Preflight PASS, FAIL and stale display | PARTIAL | WORKS | Real ready package, failed-item fixture with reason, authorized requirement change after preflight removes PASS |
| Compliance CLI and pipeline producers | WORKS | WORKS | Existing compliance tests, typed run contract reread; no writer/service changes |
| Empty Compliance recovery command | BROKEN | WORKS | Extracted rendered command accepted by real CLI parser; existing --no-ai/--force parsing retained |
| Submission web guide | BROKEN | WORKS | Every existing checklist label/detail and instruction title/description appears; actual files/recipient/deadline/timezone, portal, unknown and escaped unsafe values tested |
| Submission CLI checklist/instructions and DOCX/PDF/XLSX/ZIP/email draft services | WORKS | WORKS | Existing proposal/submission/export tests; callers/services/signatures unchanged |
| Manual confirmation gates and outcome forms | WORKS | WORKS | Real ready→submitted HTTP flow; original names/version/CSRF fields; confirmation form disappears and structured outcome controls remain |
| Browser-local submitted_at | BROKEN | WORKS | Chicago winter noon saves 18:00Z, summer noon 17:00Z; actual rendered handler execution plus stored timestamp assertions |
| UTC/offset/legacy-naive/blank submission inputs | WORKS | WORKS | Four characterization cases keep server parsing and blank/server-now behavior |
| MCP get_bid_analysis with history | PARTIAL | WORKS | Real rules-engine runs; actual protocol; newest five, unchanged keys, correct optional result status, repeat read without writes |
| MCP bid analysis with no history/missing records | WORKS | WORKS | Prior successful empty-history envelope and validation errors retained |
| Other MCP match/pursuit/proposal/outcome consumers | WORKS | WORKS | Existing MCP and workflow tests; record/status/response contracts reread and unchanged |
| Shared workspace renderer: all 13 tabs | PARTIAL due to F1 | WORKS in tested states | Existing empty-state tab test plus all tabs with actual approved proposal/ready package; state versions/statuses and audit count unchanged on GET |
| Matching/reconciliation/digest filters/semantic pursued profile | WORKS | WORKS | Reread Match/Pursuit consumers and evaluating-stage filter; existing matching/alerts/semantic tests use mocks/local data |
| Outcomes, learning, source changes, package integrity | WORKS | WORKS | Existing outcome/workflow/regression/release tests; their implementations unchanged |
| Live feeds, live models/JEV, SMTP delivery, supervised production operation | UNVERIFIED | UNVERIFIED | No live credentialed or paid/sending calls; scope unchanged from audit |

No tested feature became worse. Browser handler execution does not establish cross-browser layout/HTMX behavior; MCP protocol execution does not establish stdio supervision. Typecheck/lint remain unavailable. Full-suite evidence is authoritative for the existing suite's shared prompt bootstrap; an unseeded caller-only selection is separately investigated below rather than silently ignored.

## Final verification and baseline comparison

All seven authorized implementations are complete. **F3 remains NEEDS INPUT**, with its failure reproduced and exact contract proposal prepared above. No P0 existed in the report. Application changes are limited to five files; the pre-existing working-tree versions of `web/app.py` and `workspace/proposal.html` remain byte-identical. An isolated diff against the pre-fix working tree is saved as `audit/feature_fixes_2026-10-02/implementation.patch` so these edits can be reviewed separately from the user's extensive pre-existing changes.

| Check | Fresh baseline | Final | Comparison |
|---|---|---|---|
| Full test suite | 1,030 passed, 1 skipped, 0 failures | **1,064 passed, 1 skipped, 1 xfailed**, 0 failures; 126.86 seconds | All 1,030 original passing test identities still pass; 34 new passing cases |
| Expected failures | None | One: F3 generation failure reporting/retry | Unfixed defect, strict xfail, awaiting required contract approval |
| Skipped live test | SAM_API_KEY absent | Same SAM live test | Unchanged; no live request |
| Typecheck | `No module named mypy` | Same output | NOT VERIFIED; tool absent and no project typecheck configuration |
| Linter | `No module named ruff` | Same output | NOT VERIFIED; tool absent and no project lint configuration |
| Build | Wheel build exit 0; installed-wheel test passes | Wheel build exit 0; installed-wheel test still passes | PASS, including installed bootstrap and release lifecycle |
| Dependencies | `No broken requirements found.` | Same output | PASS |
| Touched-feature/caller audit | Original eight findings | **380 passed, 1 xfailed**, 0 failures; 68.51 seconds | Correctly seeded existing prompt registry; unchanged F3 is the expected failure |

`final-comparison.json` additionally compares the final passing test identities against every completed preservation, focused-green, per-fix full, caller and seeded audit run: **zero previously passing cases now fail**. The 35 new collected cases consist of 34 passing preservation/regression/caller cases plus the known F3 failure. No existing test file was modified. No function signature, HTTP route, response key, database schema or configuration was changed by the seven implemented fixes.

### Caller-only setup failures investigated

The first caller-only selection (375 passed, 4 failed, 1 xfailed) excluded the earlier suite's prompt bootstrap. The four failures were `test_generate_proposal_with_ai_mock`, `test_red_team_runs_and_persists_findings`, `test_red_team_major_only_does_not_block`, and `test_ai_validation_populates_side_opinion_without_changing_human_comment`. Missing active prompt versions caused placeholder drafting or missing AI validation, not changed web/MCP behavior.

To check causality, copied the source/tests into an isolated temporary directory, restored all saved pre-fix application files there, and ran the identical selection against a new disposable database. **Exactly the same four existing tests failed** (`original-caller-selection.json`); the new bug tests also failed there as expected. The working tree was never reverted or overwritten. Prepending the existing `TestPromptSystem::test_prompt_registry_sync` bootstrap test makes the feature/caller selection pass (380 passed, 1 xfailed). This is a pre-existing test-order dependency outside the eight requested findings; it is recorded rather than fixed with unrelated fixture/config changes. No introduced regression required a revert.

## Fix summary

| Finding | Fix | Tests added | Status | Risk notes |
|---|---|---|---|---|
| F1 P1 | Atomically create/reuse evaluating pursuit with creation audit under opportunity lock | 10: existing actions/refusals/stages, repeat visibility, concurrent Pursue | FIXED | Existing pursuit identity/stage/version/notes preserved; HTTP empty-200 contract retained |
| F2 P1 | Expose existing first-assignment form in empty Review branch | 5: permissions, existing controls, first assignment, start/repeat/inactive rejection | FIXED | Existing assignment POST and CSRF; no mutation on GET |
| F3 P1 | Prepared permission/version/source-checked retry and honest failure proposal | 2: retained approval/savepoint rollback; reproduced strict-xfail failure | NEEDS INPUT | Adding retry route and failure response requires user approval; defect unchanged |
| F6 P1 | Convert browser-local input to UTC ISO before posting | 6: blank/UTC/offset/legacy inputs and both Chicago seasons | FIXED | Requires browser JavaScript; handler executed in Node, cross-browser DOM/HTMX not automated |
| F8 P1 | Read optional status from persisted decision result | 2: protocol empty/missing and seven real runs/latest five/repeat no writes | FIXED | Bundles without a status field return null under the same response key |
| F4 P2 | Select matrix/preflight independently; render real ready/items and current blockers | 3: existing counts/empty states, unrelated runs/failed reasons, pass→stale | FIXED | Freshness uses the existing package/source/requirement gate; no producer changes |
| F5 P2 | Render existing checklist/instruction services, files/destination/actions | 4: original forms/gates, complete email guide, portal/unknown/escaping | FIXED | Existing forms/contracts retained; no automatic send or submission |
| F7 P2 | Display the real --opportunity-id option | 2: existing parser options and extracted rendered command | FIXED | Parser verified without running external ingestion |
| Shared renderer check | Verification only; no additional implementation | 1: all 13 tabs with actual ready state and no writes | PASS | Synthetic files and PostgreSQL only |

The detailed plan and production callers for each finding are listed at the beginning of this report; completed blast-radius checks and raw red/green/full logs follow them. Live integrations and production operation retain their previous UNVERIFIED status. There was no observed feature downgrade.
