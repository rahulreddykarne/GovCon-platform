# GovCon production readiness audit

Audit date: October 2, 2026. Target: the **current working tree**, including the substantial uncommitted implementation and test changes that existed before this review. Exact source hashes and the Git HEAD are recorded in [scope_inventory.json](audit/scope_inventory.json).

**Assessment: hold production release.** The most consequential defects can make inadequate proposal content appear compliant, retain superseded solicitation instructions, or allow commercial facts to change after approval. The built wheel also fails to start the web app.

This report records **33 findings: 26 reproduced in isolated probes and 7 traced in source that need additional validation**. Severity describes release impact, not a CVSS score. High findings should be resolved before a production pilot involving real bids. Medium findings affect reliability, accuracy, workflow usability, or deployment controls.

## Evidence and limits

| Verification | Result |
|---|---|
| Repository inventory | 137 application Python files, 31,500 lines; migrations, tests, templates, prompts, configuration and CI also examined |
| Python syntax compilation | 182 application/migration/test files compiled; no syntax errors |
| Existing tests with `GOVCON_TEST_NO_DB=1` | **420 passed, 388 skipped**, 6.96 seconds |
| Isolated defect probes | **26/26 reproduced**; [results](audit/probe_results.json), [script](audit/production_readiness_probes.py) |
| Installed dependency compatibility | `pip check`: no broken requirements |
| Installed dependency advisory lookup | OSV batch lookup: **129 packages checked, no matches**; [versions and results](audit/dependency_advisories.json) |
| Distribution artifact | Wheel built successfully; **0 templates, 0 static assets, 0 Alembic migration files**; isolated `create_app()` failed |

“Reproduced” means an executed counterexample in production functions, or an HTTP request against the real FastAPI route, with persistence/authentication/network boundaries replaced as documented in the probe script. It does **not** mean a full PostgreSQL-backed exploit or end-to-end government submission was executed. The wheel failure runs in a separate Python process against the actual artifact.

There was no available local PostgreSQL/pgvector listener or Docker/psql executable for an isolated integration environment. The unrestricted pytest invocation was stopped as it approached database-backed setup; the fixtures migrate and delete records in the configured database. The completed test run explicitly skipped those tests. No production-readiness claim should rely on its green result alone.

This was a repository-wide review with deep call-path analysis of ingestion, attachment freshness, compliance/evidence, review/approval, proposal/submission, web/MCP writes, provider fallback and scheduling. The inventory and compilation cover all source Python files; they do not establish exhaustive semantic correctness of every line. Supporting analytics, intelligence, parsers, migrations, templates and tests received targeted review. Native dependency implementations, live provider behavior, browser-rendered end-to-end workflows, PostgreSQL races, migration execution, backup restoration and load tests remain unverified.

The vulnerability-scanner skill informed trust-boundary tracing and counterexample validation. Its referenced `verification-and-triage.md` was absent from the installed skill package; findings below explicitly separate reproduction from source-only inference. No application source was changed during this audit.

## Findings reproduced in isolation

### F01 — High: failed source-change processing permanently consumes the event

**Location:** `src/govcon/workflow/invalidation.py:293`, `process_pending_source_changes`.

`_revalidate_sources` runs before `apply_source_change` inside a savepoint. An exception rolls back the work, but the loop outside the exception handler still writes `material_source_change_handled`. A temporary provider/filesystem/database error therefore prevents approval invalidation and also removes the event from later retry selection.

**Evidence:** probe F01 raises a temporary error, observes a handled marker and verifies that invalidation was never called. Source-revision checks protect some downstream actions, but do not repair the missed workflow transition or retry the event.

**Fix:** invalidate dependent approvals immediately when the event is accepted; record handled only after successful processing. Persist failed attempts separately and retry with backoff. Test an error followed by recovery, asserting old approval cannot remain usable in the interval.

### F02 — High: attachment A → B → A reversion leaves B active

**Location:** `src/govcon/enrich/attachments.py:102`, `reconcile_attachment_versions`; `_download_one` at line 273.

If the latest download matches an existing older SHA, `_download_one` returns that older row. Reconciliation then picks the highest stored row ID per URL, rather than the version just fetched. After the source reverts from B to A, B stays active and A becomes inactive.

**Evidence:** F02 executes reconciliation with the reverted A row and a higher-ID B row; B is selected.

**Fix:** reconcile using the exact URL-to-version mapping produced by the current successful fetch. Keep fetch observations distinct from immutable content versions. Cover A → B → A, repeated A, removal and re-addition.

### F03 — High: a failed attachment refresh looks like a successful current attachment

**Location:** `src/govcon/enrich/attachments.py:242`, `_record_failure`.

When a URL has any stored successful version, a refresh failure returns it without recording a failed current fetch. Reconciliation can keep that old version active. Inventory checks URL presence/hash and source events against download time; they cannot establish successful refresh of every URL, especially when the document changed in place or another file downloaded successfully.

**Evidence:** F03 fails the snapshot-2 download and receives the successful snapshot-1 row with no failure record. This establishes concealed freshness failure; a complete readiness bypass was not exercised.

**Fix:** keep historical bytes available, but record current fetch status per URL/snapshot. Unverified current bytes must block use as current evidence until retrieval or audited verification succeeds.

### F04 — High: pre-flight accepts file descriptions without real assembled files

**Location:** `src/govcon/submissions/manifest.py:24`, `verify_package`; `src/govcon/compliance/submission_preflight.py:221`; `src/govcon/cli.py:2223`.

`verify_package` checks actual bytes only when `local_path` is present. A manifest containing plausible names, sizes and hashes with no paths passes. `compliance preflight --package` directly snapshots that caller-supplied package, bypassing `assemble_package`'s path requirement. Later confirmation invokes the same permissive verifier.

**Evidence:** F04 passes a made-up proposal hash/size with no file. Other readiness checks may still block the bid; this probe demonstrates that package integrity itself gives a false pass.

**Fix:** separate a planning manifest from a verified assembled package. Pre-flight and confirmation must require retrievable content for every artifact and an assembly record tied to its immutable bytes.

### F05 — High: empty headings and explicit noncompliance produce COVERED

**Location:** `src/govcon/compliance/proposal_coverage.py:89`, `scan_coverage`, and evidence creation in `check_proposal_coverage`.

Coverage scores heading plus content using unordered token overlap. It does not require substantive matching content or distinguish affirmations from denials. A heading `ISO 9001 certification` with empty body, and `We do not have ISO 9001 certification`, both satisfy the tested certification requirement. COVERED creates verified `proposal_scan` evidence; that method can satisfy a noncritical requirement in the status gate.

**Evidence:** F05 reproduces both false positives. F06 separately exercises acceptance of proposal-scan evidence by the gate.

**Fix:** use term overlap only to locate candidate passages. Require content-backed validation of the actual obligation, including negation, quantities, dates and supporting facts; route semantic ambiguity to human review. Add negative and heading-only cases to the false-satisfied benchmark.

### F06 — High: evidence from an old proposal version satisfies the new version

**Location:** `src/govcon/compliance/matrix.py:461`, `fresh_evidence` and `inputs_for`; `src/govcon/compliance/proposal_coverage.py:131`.

Evidence freshness filters only amendment timestamps. It does not require `proposal_version_id` to match the version currently assessed. When new proposal coverage is unknown/NEEDS_REVIEW, a verified scan from the old version remains a satisfaction method. `decide_status` accepts that method despite the current coverage unknown.

**Evidence:** F06 combines v1 verified evidence with v2 unknown coverage and obtains `satisfied`.

**Fix:** pass the target proposal version into evidence selection, exclude superseded version evidence, and make current coverage ambiguity block automatic satisfaction. Test a new draft that deletes or weakens previously covered content.

### F07 — High: the same model counts as two independent critical validations

**Location:** `src/govcon/compliance/matrix.py:376`, `decide_status`; `src/govcon/compliance/validator.py:132`; `src/govcon/compliance/extractor.py:302`.

The gate counts `ai_validation` and `ai_validation_secondary` as distinct methods without checking provider/model independence. Configuration may select the same provider/model for both. The orchestration emits an independence warning but continues; two matching SATISFIED responses can satisfy a critical requirement.

**Evidence:** F07 supplies identical provider, model and evidence for both passes and obtains `satisfied` with two methods.

**Fix:** carry validator identity/provenance to the gate. Repeated calls to the same validator count as one method; require another permitted validation method for critical requirements. Apply the same rule to A/B extraction confidence.

### F08 — Medium: incomplete extraction does not retry after provider recovery

**Location:** `src/govcon/compliance/pipeline.py:107`.

`extraction_needed` checks only existence, force and inventory hash. A failed AI extraction stores an incomplete reconciliation. On an unchanged inventory, later `use_ai=True` runs skip extraction and remain incomplete indefinitely unless the operator discovers `--force`.

**Evidence:** F08 observes no AI pass calls and another incomplete result after an incomplete prior run.

**Fix:** treat incomplete/failed prerequisite runs as retryable. Cache a complete successful extraction, rather than any run with the same input hash. Test outage, policy change or key configuration followed by a normal rerun.

### F09 — High: regenerated submission instructions retain superseded values

**Location:** `src/govcon/submissions/service.py:177`, `generate_submission_package`.

Inferred method/destination/recipient/timezone/deadline are populated only when empty, without provenance distinguishing automatic values from human overrides. A changed solicitation can therefore leave the previous recipient and deadline in place. Required files are unioned with history, so removed files remain mandatory. The extracted `deadline` value is collected but not used here.

**Evidence:** F09 changes recipient and response deadline and removes one file; old recipient/deadline and the removed file persist. Pre-flight uses those persisted values and can consistently validate the wrong instructions.

**Fix:** recompute source-derived fields on source revision changes. Preserve human overrides as explicit, audited records with a controlling revision. Replace source-derived required files with the current set; detect deadline/timezone conflicts rather than silently choosing one.

### F10 — High: contradictory file-type restrictions disable the check

**Location:** `src/govcon/compliance/submission_preflight.py:85`, `collect_instructions`, and line 188 in `preflight_items`.

Allowed file types are intersected across requirements. Disjoint sets yield an empty list, indistinguishable from no restriction. The empty-list branch reports `not_applicable` on a complete inventory. Conversely, PDF-only proposal and XLSX-only pricing rules may need artifact-specific scopes rather than a global intersection.

**Evidence:** F10 combines PDF and XLSX restrictions and gets file-types NOT_APPLICABLE for an EXE artifact.

**Fix:** represent absent, allowed, conflicting and scoped restrictions separately. Contradictions must block; per-artifact rules must evaluate each matching file.

### F11 — Medium: expired semantic recommendations are labelled eligible

**Location:** `src/govcon/matching/semantic.py:39`, `is_eligible_for_pursuit`.

Semantic eligibility checks only listing status. The deterministic matcher also checks deadline, so the two paths disagree. Open listings with overdue deadlines can be labelled eligible in semantic, won-profile, pursued-profile and recompete results.

**Evidence:** F11 accepts an open listing expired 30 days earlier.

**Fix:** share deadline/status eligibility logic with deterministic matching and report a specific ineligible reason. Unknown deadlines should be explicitly labelled rather than implying confirmed eligibility.

### F12 — Medium: pipeline hides submitted and terminal progress

**Location:** `src/govcon/web/routes/__init__.py:1122`, `pipeline`.

Pipeline rendering overwrites the pursuit stage with review-session status. A review normally remains `approved_to_bid` during drafting, submission and outcomes, so the displayed card returns to Approved to Bid even when the pursuit is won, lost or submitted.

**Evidence:** F12 renders a won pursuit in the Approved to Bid column.

**Fix:** use the pursuit's stage for downstream and terminal states. Consult review status only during the review portion of the lifecycle. Add route/render tests for drafting, ready, submitted, won and lost.

### F13 — Medium: the outcome form silently discards won award value

**Location:** `src/govcon/web/templates/workspace/submission.html:82` and `:116`; `src/govcon/web/routes/__init__.py:1006`.

Won and lost sections both contain enabled inputs named `award_amount`. CSS hiding does not remove inputs from form submission. The later empty lost field wins the scalar binding, and `_float` turns it into None, losing the amount entered for a win.

**Evidence:** F13 sends the actual route `outcome=won&award_amount=95000&award_amount=`; `record_outcome` receives None.

**Fix:** disable inactive outcome fields or use unique names selected by outcome. Reject duplicate scalar financial fields and invalid numeric strings rather than silently discarding them.

### F14 — Medium: absence of an active registry prompt does not stop execution

**Location:** `src/govcon/ai/structured.py:63`, `resolve_prompt`.

All missing-active-version errors fall back to a disk-active prompt. This conflates an unsynchronized development environment with a registry that intentionally has no approved active version, or a registry path failure. The registry cannot reliably serve as the production authority.

**Evidence:** F14 supplies a registry “no active version” result and receives a disk-active prompt.

**Fix:** make disk fallback an explicit development/bootstrap mode. In production, distinguish registry absence from registry denial/error and fail closed. Also use the registry-managed runner for solicitation analysis, which currently loads directly from disk.

### F15 — Medium: approve-to-bid ignores configured alternative AI providers

**Location:** `src/govcon/web/routes/__init__.py:923`, `_trigger_proposal_generation`; `src/govcon/workflow/invalidation.py:316`; `src/govcon/proposals/service.py:155`.

Web automatic generation and default source-change AI selection test only `deepseek_api_key`. A correctly configured Anthropic or OpenAI primary provider is skipped. Separately, successful drafting records `provider="deepseek"` regardless of the actual selected provider and leaves model None, weakening version provenance.

**Evidence:** F15 configures Anthropic primary with its key and observes `skip_ai=True`.

**Fix:** resolve provider availability through the common provider factory and return actual provider/model identity with draft results. Test each supported primary through the web approval path and source-change default path.

### F16 — Medium: malformed source times crash deterministic validation

**Location:** `src/govcon/compliance/deterministic.py:137`, `parse_source_deadline`.

The time regex permits invalid hour/minute values. `datetime.replace` raises rather than returning an unknown parse result, potentially aborting the compliance pipeline for malformed or AI-extracted source values.

**Evidence:** F16 passes `25:99` and receives ValueError.

**Fix:** validate ranges and catch parsing exceptions. Return unknown with source context for human resolution. Include 24:00, invalid minutes, malformed 12-hour suffixes and naive datetime inputs in boundary tests.

### F17 — High: reviewers can alter commercial facts after approval or submission

**Location:** `src/govcon/mcp/operations.py:629`, `op_update_pursuit`.

Stage transitions are gated, but price, cost, supplier and notes updates have no comparable lifecycle restriction. A reviewer can change quote/cost/supplier on an approved, submitted or terminal pursuit. Pre-submission changes do not invalidate review/package approval, and terminal changes are treated as ordinary edits. Negative/nonfinite prices also lack service-level validation here.

**Evidence:** F17 changes a submitted pursuit's quote price with a reviewer actor. Audit logging records the change but does not preserve workflow consistency.

**Fix:** define which commercial fields may change in each stage. Before submission, substantive changes must reopen affected decisions and drafts. After submission, use an authorized append-only correction workflow tied to the submitted facts.

### F18 — Medium: login accepts cross-origin submissions; cookies lack Secure

**Location:** `src/govcon/web/routes/__init__.py:195`; `src/govcon/web/app.py:21`.

Login has no CSRF token or Origin/Referer check and accepts an attacker-account login from another origin. This can put a victim browser into the wrong account. It needs a valid invited attacker account; it is **not** evidence of arbitrary account takeover. Other POST routes also lack explicit CSRF protection, although SameSite=Lax mitigates ordinary cross-site authenticated POSTs. The cookie lacks Secure and uses a fixed 12-hour client lifetime despite configurable server TTL.

**Evidence:** F18 executes a foreign-origin login request with mocked valid authentication and observes a session cookie. A real-browser exploitation chain was not executed. [OWASP describes login CSRF and its distinct preconditions](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#possible-csrf-vulnerabilities-in-login-forms).

**Fix:** protect login and authenticated mutations with CSRF tokens and origin checks. Set Secure when deployed through HTTPS, align TTLs and add bounded login throttling. Validate same-site/different-port origins as well as ordinary external origins.

### F19 — High: packaged deployment cannot start the web application

**Location:** `pyproject.toml:48`; `src/govcon/web/app.py:17`; `src/govcon/web/routes/__init__.py:74`; `src/govcon/paths.py:6`.

Package data includes prompts and clause JSON, but excludes web templates/static files. The actual built wheel contains neither, and `StaticFiles` fails during app construction. Migrations and repo-root-dependent CLI resources are also absent, so installed-artifact database/bootstrap flows need a deployment design.

**Evidence:** F19 extracts the built wheel into a temporary directory, imports it first in an isolated subprocess, and observes missing-static-directory failure. Checkout-based editable tests cannot detect this.

**Fix:** ship assets and migration resources using package-aware paths, or define and test a source-based deployment artifact that contains them. Add clean installed-artifact startup and database-upgrade smoke checks to CI.

### F20 — Medium: semantic post-filtering can return zero despite valid candidates

**Location:** `src/govcon/matching/semantic.py:68` and `:182`.

The watchlist caller asks for `limit + matched_count`, but `_vector_search` caps retrieval at 50. Filtering rule matches afterwards can discard all results while candidate 51 remains available. Exclusion also includes inactive/dismissed historical match rows, potentially hiding relevant recommendations.

**Evidence:** F20 supplies 50 previously matched nearest neighbors plus another candidate; ten requested recommendations return zero.

**Fix:** apply existing-match exclusions in SQL before LIMIT, with an explicit status/active policy. Bound the public result count separately from candidate retrieval. Test watchlists with more than 50 match records.

### F21 — High: SMTP authentication and delivery silently proceed without TLS

**Location:** `src/govcon/alerts/digest.py:223`, `send_smtp`.

For non-465 SMTP, STARTTLS is attempted only if advertised. If absent, the client still logs in and sends the digest. Against a remote server or a downgraded connection, this can expose credentials and bid-related alert data. Local trusted relay configurations have different exposure.

**Evidence:** F21 uses an SMTP client that does not advertise STARTTLS; login and send both execute. [Python documents STARTTLS as the operation that encrypts subsequent SMTP commands](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.starttls).

**Fix:** require authenticated TLS for remote credentialed SMTP with a certificate-validating SSL context. Allow a plain local relay only through explicit, narrowly scoped configuration. Failed negotiation must leave alerts unmarked.

### F22 — High: prompt sync bypasses the advertised activation gate

**Location:** `src/govcon/prompting/registry.py:46`, `_upsert_prompt`, and `:202`, `load_prompt`.

`sync_prompts` activates every disk-active asset through `activate_version`, bypassing `activate_prompt` and its regression/audit gate. Loading an active registry row also rereads mutable disk content without verifying it matches the approved registry hash/version. An edited prompt can therefore execute without a newly recorded approval.

**Evidence:** F22 observes activation of a safety-critical prompt during sync with no activation-gate call. This is an operator/local release-control issue, not a remote privilege escalation.

**Fix:** sync immutable versions as inactive, enforce content hash/version uniqueness and load the exact approved content. Route every activation path through the same gate and audit event. Test edited-in-place and newly synced versions.

### F23 — Medium: malformed JEV responses defeat provider fallback

**Location:** `src/govcon/decision/providers/jev.py:104`; `src/govcon/decision/engine.py:342`.

Transport/HTTP failures become `DecisionProviderUnavailable`, which the engine handles. HTTP-200 invalid JSON, non-object JSON and malformed cost fields can instead raise parsing/type exceptions outside that conversion. The rules fallback is then skipped and dependent workflow processing may abort.

**Evidence:** F23 returns HTTP 200 with invalid JSON and observes JSONDecodeError escaping the provider boundary.

**Fix:** validate the entire provider response inside the availability/error boundary and convert malformed responses to a typed provider error with safe details. Test non-JSON, arrays, absent answers, invalid cost and nonfinite confidence.

### F24 — High: removed attachment text is still analysed as current

**Location:** `src/govcon/enrich/summarize.py:91`, `run_solicitation_analysis`; `src/govcon/workflow/source_revision.py:25`.

The summary file query filters extraction success/partial but not `StoredFile.active`. Removed/replaced historical attachments are fed into the new summary and stamped with the current revision. The revision hash itself uses all stored SHA values rather than the controlling active URL/version mapping, weakening attachment-membership freshness checks.

**Evidence:** F24 verifies the missing query predicate and observes a removed file's text reaching the provider prompt.

**Fix:** build summaries and revision stamps from the same authoritative current inventory. Include active membership and selected content versions in revision identity; retain history outside the current analysis context.

### F25 — High: the buyer's delivery requirement is used as supplier lead time

**Location:** `src/govcon/decision/engine.py:194`, `:239`, `:252`; `src/govcon/decision/providers/rule_fallback.py:146`.

Both `opportunity.required_delivery_days` and `sourcing.lead_time_days` receive `summary_json.delivery.delivery_days`. With a known required delivery period, the hard-rule comparison is effectively the same number against itself. Rule fallback can report delivery feasible even when no supplier lead-time evidence exists.

**Evidence:** F25 builds production decision state with no pursuit/supplier data and a 30-day buyer requirement; both fields become 30 and feasibility becomes yes.

**Fix:** store verified supplier lead time separately with quote/source/date provenance. Leave it unknown when absent. Test required delivery 30 days with unknown, 20-day and 60-day supplier lead times.

### F27 — High: proposal coverage accepts a version from a different opportunity

**Location:** `src/govcon/compliance/proposal_coverage.py:131`; `src/govcon/cli.py:2198`.

Coverage accepts opportunity ID and proposal-version ID independently. It loads the version but does not verify its parent proposal belongs to the opportunity. It can attach evidence and clear coverage findings for opportunity A based on proposal B, including an accidental CLI ID mismatch. This is a referential-integrity defect; the CLI caller already has local database access.

**Evidence:** F27 supplies a foreign proposal ID on the version and the coverage service accepts it without loading its owner. Database-backed false satisfaction with real A/B rows remains an integration test to add.

**Fix:** verify the complete opportunity → proposal → version relationship before any writes. Apply equivalent ownership checks to export and package assembly.

## Additional source-traced findings requiring validation

### F26 — High: package bytes are not bound to the approved proposal content

**Location:** `src/govcon/submissions/manifest.py:68`; `src/govcon/compliance/submission_preflight.py:248`; `src/govcon/proposals/service.py:635`.

Assembly hashes whatever local file the caller supplies while trusting its `role` and `proposal_version_id`. Coverage evaluates DB proposal sections. Neither assembly nor pre-flight establishes that the file labelled proposal actually contains that immutable version's approved content. Confirmation compares IDs, not artifact provenance. A correctly hashed wrong/empty document can therefore differ from the text reviewed.

**Confidence:** traced integration risk; no full final-approval counterexample executed. This is separate from F04: requiring a local file alone does not bind its content to the approved version.

**Fix:** export the proposal from the pinned version and persist artifact provenance/hash. For signed or edited final artifacts, require a dedicated content-review approval of those exact bytes. Add an end-to-end test substituting a different file while retaining the claimed version ID.

### F28 — High: opposing row-lock orders can deadlock submission changes

**Location:** `src/govcon/proposals/service.py:397`, `finalize_proposal`, and `:562`, `record_submission_confirmation`; `src/govcon/workflow/invalidation.py:170`.

Final proposal actions lock proposal then pursuit, and RETURN_FOR_FIX subsequently locks submission while invalidating readiness. Submission confirmation locks opportunity then submission, then pursuit after reading the committed proposal approval. Concurrent return-for-fix and confirmation can each hold the row the other needs.

**Confidence:** suspected PostgreSQL deadlock from the concrete lock graph; no two-connection test executed.

**Fix:** serialize opportunity workflows using a consistent opportunity-first locking convention, then lock child rows in one documented order. Validate concurrent return/confirmation and source-change/approval transactions with real PostgreSQL barriers and bounded timeouts.

### F29 — Medium: every session creates a new engine and pool

**Location:** `src/govcon/db.py:31`; authentication and render helpers in `src/govcon/web/routes/__init__.py`.

`session_scope` builds a new engine/session factory for every call and closes only the session. A page may invoke several such scopes. This defeats connection-pool reuse and leaves engine/pool cleanup to garbage collection, causing connection churn and possible resource pressure under sustained load. Connection exhaustion was not measured. [SQLAlchemy recommends one engine per database URL per application process](https://docs.sqlalchemy.org/en/20/core/connections.html#basic-usage).

**Fix:** manage an engine/sessionmaker per configured process or app lifetime and dispose it at shutdown. Use the same app-injected settings throughout routes; currently `create_app(settings)` stores settings while route scopes generally use global settings. Load-test connection counts and custom-settings isolation.

### F30 — Medium: AI budget settings do not enforce an overall opportunity budget

**Location:** `src/govcon/config.py:91`; `src/govcon/compliance/extractor.py:336`; `src/govcon/ai/structured.py:113`; `src/govcon/enrich/summarize.py:182`.

Extraction uses the input setting as a per-pass character estimate. General structured prompts concatenate all variables; summaries cap each file separately; there is no aggregate token/cost accounting across drafting, validators, retries and analyses. `ai_max_cost_usd_per_opportunity` does not control provider calls. Large contexts and repeated requests can exceed configured intent or provider limits.

**Confidence:** traced missing enforcement, not a measured bill or denial of service.

**Fix:** introduce central token/output limits, persisted usage accounting, per-opportunity budget reservation and retry accounting. Bound combined context and preserve omitted-source warnings. Test multi-file inputs and repeated calls crossing the budget.

### F31 — Medium: release gates do not validate the behavior they appear to certify

**Location:** `src/govcon/prompting/evaluation.py:123`; `src/govcon/compliance/regression.py:139`; `scripts/smoke.sh:170`; `.github/workflows/pytest.yml`.

The activation gate replays fixed recorded outputs without passing the candidate prompt body into behavioral evaluation. Its injection check verifies message placement, not whether a model obeys injected instructions. The smoke script writes approved state directly and accepts a blocked checklist, so it does not exercise human quorum → real assembly → final approval → confirmation. CI tests editable checkout installation and misses F19.

**Confidence:** traced validation gap; existing structural/replay tests still provide value, but their scope is narrower than behavioral acceptance.

**Fix:** retain deterministic replay tests, add prompt-version-sensitive evaluations, and run an isolated full lifecycle through public/service gates with real generated artifacts. Add installed-package, failure recovery, negative evidence and concurrent workflow release tests. Never run the destructive fixtures against a shared operational database.

### F32 — Medium: stored notifications have no functioning user-facing inbox

**Location:** `src/govcon/web/routes/__init__.py:182`; `src/govcon/web/templates/base.html:28`; `src/govcon/web/app.py:21`.

The base template displays an unread count and links it to `/notifications`, but that destination is not registered. The reviewed web routes expose no notification listing or read/mark-read workflow. Users can see that notifications exist but cannot open their contents or clear them through this UI. Other configured delivery channels may still deliver some alerts.

**Confidence:** direct broken-link/router gap; browser flow not exercised.

**Fix:** implement an authenticated notification inbox plus mark-read actions, or provide another explicit delivery path and remove dead links. Test assignment/amendment notification visibility for its intended recipient.

### F33 — High: local source documents cannot carry classification into AI calls

**Location:** `src/govcon/enrich/attachments.py:334`, `process_local_file`; `src/govcon/enrich/summarize.py:131`; `src/govcon/compliance/pipeline.py:58`; `src/govcon/security/classification.py:17`.

The gateway correctly blocks explicit FCI/CUI classifications by default, but the local-file ingest path records no classification. Solicitation/requirement extraction call paths label source documents PUBLIC, including files imported locally. A proprietary or controlled document attached through that path therefore has no supported classification metadata that the caller can propagate to the gateway. Public government-feed content and manually imported internal content are different trust boundaries.

**Confidence:** traced policy/data-model gap. No real proprietary, FCI or CUI content was sent externally in this audit. Exposure requires such content to be imported and a provider to be enabled.

**Fix:** require classification and source origin on document ingest; propagate the strictest included classification to every external call. Add synthetic proprietary/FCI/CUI local-file tests that prove provider calls are blocked under default policy.

## Repair order and acceptance plan

1. **Compliance and artifact integrity:** F04–F07, F10, F26–F27. Demonstrate that an empty/negative/old-version/foreign-version proposal and a substituted or nonexistent artifact cannot obtain a clean readiness result. Overrides must identify the exact facts and bytes they bypass.
2. **Source and approval consistency:** F01–F03, F09, F17, F24–F25. Demonstrate amendment failure/recovery, A → B → A attachment selection, updated deadlines/destinations, real supplier lead times and commercial-change invalidation.
3. **Deployability and security:** F18–F19, F21–F22, F28–F29, F33. Start the actual release artifact, require transport security where deployed, enforce prompt/classification policy and prove bounded concurrent transactions against an isolated PostgreSQL/pgvector database.
4. **Workflow reliability and UX:** F08, F11–F16, F20, F23, F30–F32. Exercise provider recovery, every pipeline stage, form submission semantics, notification delivery and budget limits.

For each fix, convert the audit counterexample into a regression test that asserts the corrected behavior. The current probe assertions deliberately assert the defect and must not be used as passing release tests. Use PostgreSQL-backed tests for invariants involving transaction isolation, constraints, lock ordering and state changes across services.

Additional release work: lock deployment dependencies and constrain incompatible major upgrades (notably the APScheduler 3.x APIs); update the README that still says implemented features are future phases; test migration upgrade against both empty and legacy representative databases; verify backup restoration, observability, external-service timeouts, workload limits and installation from a clean environment. The OSV result concerns the installed versions at audit time and does not guarantee future unpinned installs or absence of unpublished vulnerabilities.

## Re-running the isolated evidence

Run from the repository root in PowerShell:

```powershell
$env:GOVCON_TEST_NO_DB = '1'
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m pip check

# F19 needs the actual wheel at this temporary location.
$auditWheelDir = Join-Path $env:TEMP 'govcon-production-audit-wheel'
.venv/Scripts/python.exe -m pip wheel . --no-deps --no-build-isolation --wheel-dir $auditWheelDir
.venv/Scripts/python.exe docs/audit/production_readiness_probes.py
```

Probe output and source fingerprints are preserved under `docs/audit/`. Probes use synthetic data and mocks, temporary file extraction for the wheel, and no external provider or SMTP requests. The OSV advisory lookup submitted public package names/versions only.
