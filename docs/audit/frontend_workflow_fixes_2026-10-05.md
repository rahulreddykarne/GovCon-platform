# Frontend and workflow review implementation

Status: implementation complete and verified on the final frozen source tree. This report covers the supplied frontend/workflow review, rather than a fresh production certification of every repository feature.

## Verdict

The actionable review items were traced against the current code before implementation. Five reported bugs were already repaired by earlier work and were retained with regression coverage. The remaining workflow bugs and the requested navigation, workspace, watchlist, sourcing, notification, account-management, and progress improvements have been implemented. A final page trace caught and repaired the missing Pursue control in the merged workspace, invalid notification glyph, and duplicate script markup in the Inbox title. Full-suite verification exposed truncation beyond 100 unique matches; bounded pagination now keeps all matches reachable and counts the complete matched set. Malformed SAM contact entries and legacy dictionary contacts now render safely on the merged Overview.

Existing working-tree changes were preserved. Verification completed before publication, against working-tree fixes on base commit `9d81537763162787c3f1da3320edcebf7d1cc392`. The completed project is published on branch `fix/audit-and-workflow-2026-10-05`. Tests use disposable PostgreSQL databases on port 55432 and mocked providers; no real provider, email, payment, or submission calls were made.

## Baseline and final checks

| Check | Baseline at start of this review | Final |
|---|---|---|
| Strict Ruff/mypy gate | `ruff: 0 diagnostics`, `mypy: 0 diagnostics`, `Static gate: PASS` | Same output; PASS |
| Full pytest suite | `1 failed, 1412 passed, 1 skipped, 200 warnings in 330.78s (0:05:30)` | `1443 passed, 1 skipped, 212 warnings in 393.99s (0:06:33)` |
| Focused final web checks | Not applicable | `120 passed, 9 warnings in 21.16s` |
| Mypy with Linux platform | Not applicable | `Success: no issues found in 195 source files` |
| JavaScript progress tests | New coverage | 4 passed, 0 failed |
| Dependency integrity | Not applicable | `No broken requirements found.` |
| Installed-wheel startup and lifecycle | Existing regression coverage | PASS: installed startup/migrations, local assets, lifecycle, recovery, and concurrent approvals |
| Visual browser verification | Not available | UNVERIFIED: computer-use inventory has no browser surfaces |

The single baseline failure was `test_registration_refresh_overlays_facts_and_alerts_before_expiry`: its expiry date used local `date.today()` while the service calculates in UTC. The test now uses a fixed UTC clock across midnight. Production date behavior was preserved; the focused regression passed.

## Review findings and disposition

Numbers correspond to the supplied review.

| # | Severity | Category | Location / symbol | Finding and disposition | Confidence |
|---|---|---|---|---|---|
| 1 | P1 | Workflow | `src/govcon/web/templates/workspace/_outcome.html`; `src/govcon/web/routes/workspace.py:workspace`; `src/govcon/learning/outcomes.py:record_outcome` | Cancellation was hidden behind submission gates; submitted outcomes offered an invalid No bid transition. Independent outcome controls now follow the transition table and cancellation stops active work. FIXED. | CONFIRMED |
| 2 | P1 | Data integrity | `src/govcon/collaboration/review_sessions.py:_sync_pursuit_stage` | Review No bid analytics were already repaired: the shared outcome service records feedback and reopening removes the current no-bid count. VERIFIED ALREADY FIXED. | CONFIRMED |
| 3 | P2 | Presentation | `src/govcon/web/templates/workspace/overview.html`; `_decision_display.html` | Decision scores use percentage conversion and explicit None checks, including zero. VERIFIED ALREADY FIXED. | CONFIRMED |
| 4 | P2 | Presentation | `src/govcon/web/templates/workspace/_decision_display.html:items_of` | Strengths and risks render as readable lists instead of raw structured JSON. VERIFIED ALREADY FIXED. | CONFIRMED |
| 5 | P2 | Permissions | `src/govcon/web/routes/workspace.py:workspace`; `src/govcon/web/templates/workspace/review.html` | Unassigned reviewers were shown a comment form that the service rejects. The form now requires the actor's assignment. FIXED. | CONFIRMED |
| 6 | P1 | Reliability | `src/govcon/collaboration/comments.py:add_comment`; `src/govcon/collaboration/comment_tasks.py`; `src/govcon/tasks/handlers/analysis.py` | Web comment submission ran external AI work inside its transaction. It now saves the comment and durable task, and the worker executes the provider outside its publish transaction. Changed evidence supersedes and requeues validation. FIXED. | CONFIRMED |
| 7 | P1 | Workflow | `src/govcon/web/workspace_actions.py`; `src/govcon/web/templates/workspace/proposal.html` | Proposal return-for-fix, revision/red-team, and regeneration controls were already implemented with end-to-end regression coverage. VERIFIED ALREADY FIXED. | CONFIRMED |
| 8 | P2 | Performance / workflow | `src/govcon/web/routes/discovery.py:pipeline` | Pipeline duplicated matches, performed per-card review queries, and counted closed pursuits as active. Joined/batched queries, six columns, unique matches with bounded pagination, complete matched counts, separate closed totals, and outcome filters repair these paths. FIXED. | CONFIRMED |
| 9 | P2 | Search | `src/govcon/web/routes/discovery.py:search` | Deadline descending / NULL-first ordering hid urgent results and text search lacked relevance ranking. Relevance now sorts first, then earliest dated deadline, undated last, with a stable ID tie-breaker. FIXED. | CONFIRMED |
| 10 | P2 | Input validation | `src/govcon/web/routes/watchlists.py:_watchlist_save_sync`; watchlist templates | Invalid filters are rejected with preserved form values and zero filters render correctly. VERIFIED ALREADY FIXED; checkbox support and automatic matching were added. | CONFIRMED |
| 11 | P1 | Workflow / permissions | `src/govcon/web/routes/discovery.py:inbox_action`; `src/govcon/web/templates/inbox.html` | HTMX failures were silent, read-only users saw mutation controls, and Pursue did not navigate. Errors now render honestly, controls follow permissions, and Pursue redirects to the workspace. One card represents all matching watchlists; mutations update all matches. Bounded pages retain every unique opportunity beyond the first 100. FIXED. | CONFIRMED |
| 12 | P2 | Message integrity | `src/govcon/web/routes/common.py:_redirect` / `_render`; `src/govcon/web/templates/base.html` | Arbitrary query-string messages could impersonate server notices and repeat on reload. Signed, short-lived, session-bound flash cookies now display once; query strings carry only boolean markers. FIXED. | CONFIRMED |
| 13 | P2 | API contract | `src/govcon/collaboration/review_sessions.py:complete_assignment`; `src/govcon/collaboration/comments.py`; review template | Contradictory No bid/Bid choices and `needs_info` drift were allowed. The UI separates action from one canonical recommendation; the service rejects contradictions and accepts legacy aliases. FIXED. | CONFIRMED |
| 14 | P1 | Workflow / data contracts | `src/govcon/ingest/sam_opportunities.py:_point_of_contact_payload`; `src/govcon/web/templates/workspace/_source_details.html:45` | An accepted contact list containing strings/nulls, or legacy dictionary contacts, caused `.get` failures and broke Overview. The merged renderer checks mapping types, displays escaped plain values, and skips nulls. FIXED. | CONFIRMED |

## Trigger, impact, and repair evidence

**1 — Outcome controls.** An approver with an evaluating/drafting pursuit previously had no cancellation form until submission; a submitted pursuit was offered a No bid action that fails its state transition. The form is now independent of submission approval, its options use `can_transition`, and cancelling an opportunity cancels active preparation/analysis/proposal/quote tasks. Regressions exercise early cancellation, submitted options, read-only access, and out-of-range margin defaults.

**6 — Comment validation.** Posting a substantive comment previously awaited an external provider before committing its human text, holding the request and transaction open. The web path now passes `defer_ai_validation=True`; the existing durable `ai_analysis` worker handles a reviewer-comment payload. The worker checks a hash of the complete prompt inputs before prepare and publish, preserves the human text, and publishes a validated AI side opinion. A regression changes supplier evidence during the mocked provider call and proves the first result is superseded, then the current result is published.

**11 — Inbox actions.** A denied/missing-match/invalid action returned an HTMX error that was never swapped into the page. A Pursue success removed a card without opening the created workspace. Error responses now contain escaped alert markup and client error handlers display it. Pursue uses `HX-Redirect`; normal form submissions use a 303 redirect. SQL deduplicates before the 100-card limit and labels all matching watchlists; a single mutation updates all matches for that opportunity.

**14 ? Contact payloads.** SAM normalization accepts `pointOfContact` lists without validating each element, and the stored JSON field also admits legacy dictionaries. The copied POC block iterated keys or scalar elements and called `.get`, raising Jinja `UndefinedError` on the Overview route. The repaired template treats a dictionary as one contact, guards dictionary lookup per item, skips nulls, and escapes remaining values. Five database-backed web cases exercise string, mixed/null, legacy dictionary, and hostile-text payloads through `/opp/{id}` and `/workspace/{id}`; the focused contact/workspace run reported `9 passed, 20 deselected in 5.63s`.

The other repaired P2 paths have focused regressions in `tests/test_web_review_cleanup.py`; previously fixed No bid analytics, score formatting, structured lists, proposal revision, and watchlist validation retain stronger service/web tests in `tests/test_audit_remediation.py` and related suites.

## Journey map

| Journey | Trace and former breakpoints | Evidence |
|---|---|---|
| Discover and pursue | Inbox/Search/Pipeline → unified opportunity workspace → explicit Pursue POST → shared pursuit creation → queued preparation. Duplicate Inbox cards, silent failures, and missing navigation repaired. Role-gated header Pursue is covered by GET-without-side-effects and repeat-POST regressions. | Database-backed web tests; shared preparation tests |
| Prepare and review | Preparation task → requirements/compliance and analysis → assignment → human comment → queued AI side opinion → canonical recommendation → approval. Inline AI transaction and unassigned comment control repaired. | Worker drain with fake provider; evidence changes during execution; permissions tests |
| Source and price | Supplier quote → validity/quantity/unit checks → Use this quote → optimistic commercial update → dependent review invalidation. Supplier/cost selection preserves proposed selling price. | Valid, expired, foreign-opportunity, and stale-version quote regressions |
| Proposal through outcome | Approval → draft/revision/red-team/regenerate → submission readiness → recorded submission → Won/Lost, or early Cancelled. Invalid No bid outcome option and hidden cancellation repaired. | Existing lifecycle/proposal tests; new outcome regressions |
| Configure and administer | Watchlist checkbox filters → validation → save and matching → delete with pursuits retained; owner account action → serialized last-owner check → session revocation and audit. | Watchlist and user-management web/service regressions |

## Feature verification matrix

WORKS below means exercised with isolated test data or complete trace as stated, rather than a claim about production integrations.

| Feature | Entry point | Expected behavior / writes | Verified by | Status |
|---|---|---|---|---|
| Seven main navigation links and Admin grouping | Base template; `/admin` | Inbox, Pipeline, Search, Watchlists, Suppliers, Insights, Admin; Settings/Operations/Users beneath Admin | TestClient rendering and route trace | WORKS; visual layout unverified |
| Unified seven-tab workspace and legacy aliases | `/opp/{id}`; `/workspace/{id}?tab=...` | Original information retained; one editable commercial form; legacy URLs still resolve | Rendered route tests including aliases | WORKS |
| Inbox deduplication, role controls, Pursue redirect | `/`; `/inbox/action` | One opportunity card; all related Match statuses updated; Pursuit and Task created only on POST | Database-backed web regressions including 105 doubly matched opportunities across pages | WORKS |
| Six-column Pipeline and Closed filter | `/pipeline` | Actual stage remains visible; unique matched cards; closed excluded from active total | Rendered database-backed regressions and pager traversal | WORKS |
| Ranked search | `/search` | Bound full-text query ranked by relevance, then urgency | Seeded relevance/date fixtures | WORKS |
| Assigned reviewer comments and durable AI validation | Review tab; POST comment | Human comment saved promptly; one durable task; current AI opinion published | Web request + worker execution with mocked provider and changed evidence | WORKS |
| Canonical review recommendations | Review tab; complete-review POST | No contradictory No bid/Bid choices; legacy `needs_info` normalized | Service validation and route tests | WORKS |
| Structured No bid learning | Approval POST; Insights | One current feedback outcome, no double count, reopen changes current analytics | Existing regression | WORKS |
| Source-details contact variations | Overview on `/opp/{id}` and `/workspace/{id}` | Accepted mixed/null SAM lists and legacy dictionaries render without a 500; valid email links remain; unsafe text is escaped | Five DB-backed route cases after synthetic normalization trace | WORKS |
| Independent outcome controls and defaults | Submission tab; record-outcome POST | Transition-valid Won/Lost/Cancelled choices; cancellation stops work; known fields prefilled | New outcome regressions | WORKS |
| Proposal correction and regeneration | Submission tab | Save revision, red-team, and regenerate through the existing service | Existing end-to-end regression | WORKS |
| Watchlist checkboxes, validation, counts, automatic matching, delete | `/watchlists` | Preserve invalid input, save sources/set-asides, rebuild matches in transaction; deletion leaves pursuits | Web + DB tests and complete delete trace | WORKS |
| Use this quote | Sourcing tab; quote use POST | Only usable current quote for this opportunity updates supplier and sourcing cost at expected version | Web tests for valid/expired/foreign/stale quotes | WORKS |
| Plain-language notifications and local HTMX | Notifications; static route | Relevant links and sentences, no raw payload, no runtime CDN dependency | All notification types rendered; local asset served | WORKS |
| One-use genuine server messages | Shared redirects | Signed message bound to session, tamper-resistant, consumed on next page | URL spoof and repeated-load regressions | WORKS |
| Owner user management | `/admin?section=users`; account action POST | Change role, deactivate/reactivate, reset password, revoke sessions, protect last owner | Web/service regressions; concurrency locking traced | WORKS |
| Automatic progress with edited-form preservation | Progress GET; `workspace.js` | Authenticated read-only snapshots; retry failed fetch; refresh statuses while retaining edited form | DB endpoint test + four Node behavior tests | WORKS in test harness; real browser unverified |
| Shared permission helper and dataclass presentation | `can()`; `ViewRow` | UI and service agree on actions; named presentation model | Strict typing, rendered web tests, complete call trace | WORKS |
| Route module split | `src/govcon/web/routes/` | Public exports preserved; separate discovery, workspace, review, proposal, sourcing, account, watchlist, operation, settings modules | Web regression suites and strict gate | WORKS |

## Readiness checklist

| Item | Result | Evidence |
|---|---|---|
| Lint and typecheck introduce no diagnostics | PASS | Strict gate; native and Linux-platform mypy |
| Full suite passes on frozen source | PASS | JUnit: 1,444 cases, 0 failures/errors, 1 skip; all 446 source/config/test hashes unchanged |
| Authenticated/authorized new mutations | PASS | Server-side `can`/permission checks and fresh user loading; read-only regressions |
| CSRF on new forms | PASS | Existing CSRF middleware; forms include tokens; CSRF-enforcing test client |
| Idempotent pursuit creation and optimistic quote updates | PASS | Shared pursuit creation, opportunity lock, expected-version service; repeat POST regressions |
| External AI outside web transaction | PASS for web comment path | Deferred task regression and worker prepare/execute/publish trace |
| No schema migration needed for comment tasks | PASS | Existing `ai_analysis` type and kind-dispatched payload |
| Cancellation prevents publication of active work | PASS in covered paths | Shared task cancellation; cancelled-pursuit publication guard |
| Owner management does not expose credentials | PASS | Password hashing, session revocation; audit excludes passwords and hashes |
| Local static assets packaged | PASS | Installed wheel serves CSS, workspace JavaScript, HTMX, and its license outside the checkout |
| Existing user changes preserved | PASS | Initial baseline retained; final 446-file source fingerprint unchanged throughout the complete suite; HEAD unchanged |
| Visual/responsive/browser interactions | NOT VERIFIED | No browser surface available through computer-use |
| Real external integrations and production deployment | NOT VERIFIED | No paid providers, emails, live submissions, or production DB mutations used |

## Not verified and practical limits

The suite still reports 212 dependency/framework warnings, chiefly Starlette per-request-cookie deprecation and Pydantic unsupported-field-attribute warnings. These are recorded rather than hidden; lint/typecheck diagnostics and test failures are zero.

- Visual layout, browser DOM/HTMX interaction, and actual responsive behavior could not be exercised: computer-use returned an empty browser inventory. TestClient checks HTML/HTTP behavior and Node tests exercise progress logic, but these do not substitute for browser visual verification.
- The only skipped test is `tests/test_sam_ingestion.py:test_live_pull_yields_at_least_one_record`, with the actual reason `SAM_API_KEY is not set`. Live SAM/DIBBS/provider/SMTP integrations and production databases were not exercised. Provider behavior in this review uses mocks; real submissions/emails were not sent.
- Concurrent multi-process owner actions are serialized by a PostgreSQL advisory transaction lock in the new service; the guard and sequential edge cases are tested, but a dedicated two-process account-management race test was not run.
- Production performance, production-sized migrations, backup/restore, and hosting/proxy configuration were outside this supplied frontend review. The complete installed-artifact/lifecycle suite covers the existing isolated deployment path.

## Additional corrections caught during verification

- At `src/govcon/web/templates/workspace.html:14`, the merged workspace header now provides a CSRF-protected Pursue form when the actor can review and no pursuit exists. GETs remain read-only; repeated POSTs create one pursuit and one preparation task. Owner, reviewer, and read-only cases pass.
- In `src/govcon/web/routes/discovery.py:inbox` / `pipeline`, Inbox and matched Pipeline cards now provide bounded pagination with stable ordering and accurate full-set counts. Tests seed 105 opportunities with duplicate matches, navigate the rendered Next links, check no opportunity repeats across pages, and exercise invalid and past-end page numbers. Existing display tests now navigate the pager to their fixture rather than assume every fixture appears on page one.
- At `src/govcon/web/templates/inbox.html:2`, Inbox error-handler JavaScript appears only in the body; a regression rejects script markup inside the title. The notification bell uses an HTML character entity to avoid an invalid literal `??` glyph.
- Tests for prior contracts now verify the intended behavior: HTMX Pursue returns `HX-Redirect`, ordinary forms return 303, signed notices are asserted on the destination page, notifications show typed sentences and preserve recipient isolation, and submitted outcomes exclude No bid. Data writes, idempotency, concurrency, permissions, and denial assertions remain.
- The first complete run reported `18 failed, 1415 passed, 1 skipped, 207 warnings in 395.77s (0:06:35)`. These failures were inspected individually; recovered affected suites reported `1 failed, 169 passed, 110 warnings in 108.76s (0:01:48)` with only an updated notification-form assertion remaining. That assertion was corrected to the existing action-required Acknowledge form; its focused regression reported `1 passed, 25 deselected in 3.06s`.
- The complete suite before the last contact guard reported `1438 passed, 1 skipped, 210 warnings in 396.90s (0:06:36)`. Its output is preserved as `pre-contact-tests.log` / `.xml`; the final complete suite includes all five added contact cases.
- Installed-wheel startup now explicitly checks `workspace.js`, local `htmx.min.js`, and its license outside the checkout. Runtime dependencies, migrations, and the existing isolated release lifecycle remain covered by the complete suite.

## Verification artifacts

Publication cleanup removes surplus EOF blank lines from typing stubs and the outcome partial; non-newline content is unchanged and the strict static gate is rerun before pushing.

The raw verification logs and manifests are retained locally in the Codex audit scratch directory; the exact outcomes are recorded here. Generated analysis caches and local runtime data are excluded from publication.

- `baseline-static.log`, `baseline-tests.log`, `baseline-tests.xml`: initial checks.
- Per-fix red/green logs: failing regression evidence followed by passing checks.
- `final-targeted.log`: 120 passing focused checks.
- `final-static.log`, `final-mypy-linux.log`: zero-error static checks.
- `final-tests.log`, `final-tests.xml`: final complete-suite results: 1,443 passed, 1 skipped, no failures/errors.
- `final-suite-manifest.json`: hashes of 446 source/config/test files captured immediately before the final full run; generated egg-info and Python caches are excluded. `final-source-verification.json` confirms every hash stayed unchanged.
- `contacts-red.log` / `contacts-green.log`: contact-shape regressions before and after the guard.
- `first-release-tests.log` / `.xml`: preserved first complete-run failure evidence; subsequent regression logs record each correction.
- Disposable browser helper stopped and its UUID test database removed after browser availability was ruled out.
