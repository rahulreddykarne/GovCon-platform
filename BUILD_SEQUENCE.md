# GovCon v2.5 — Build Sequence

**Master source of truth:** `MASTER_SPEC_v2.5.md`

Use the phase files one at a time. Give the coding agent the master spec plus the current phase spec. Do not ask it to implement future phases in the same run.

## Recommended implementation order

| Order | Phase file | Main outcome | Hard gate before next phase |
|---:|---|---|---|
| 1 | `PHASE_00_FOUNDATION.md` | Repository, database, configuration, authentication baseline, auditability, prompt/JEV-ready project structure. | Phase tests + all acceptance criteria pass; dependencies: None |
| 2 | `PHASE_01_SAM_INGESTION.md` | SAM.gov ingestion, immutable snapshots, diffs, contacts, archive handling, source fidelity. | Phase tests + all acceptance criteria pass; dependencies: 0 |
| 3 | `PHASE_02_MATCHING.md` | Watchlists, deterministic matching, explainable shortlist generation. | Phase tests + all acceptance criteria pass; dependencies: 0, 1 |
| 4 | `PHASE_03_ALERTS.md` | Alert digest and amendment re-alert behavior. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2 |
| 5 | `PHASE_04_DIBBS.md` | DIBBS verification, ingestion, NSN/quantity normalization, idempotency. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2 |
| 6 | `PHASE_05_AWARDS_PRICING.md` | USAspending award history and pricing intelligence. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2 |
| 7 | `PHASE_06_VENDORS_COMPETITORS.md` | Vendor/entity profiles, buyer contacts, competitor intelligence. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 5 |
| 8 | `PHASE_07_ATTACHMENTS_AI_ANALYSIS.md` | Attachment ingestion, DeepSeek/AI provider layer, prompt registry, structured solicitation analysis. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 5, 6 |
| 9 | `PHASE_08_JEV_DECISION_PACKAGE.md` | JEV decision provider, all decision bundles, preliminary bid recommendation, AI decision package. | Phase tests + all acceptance criteria pass; dependencies: 0, 2, 5, 6, 7 |
| 10 | `PHASE_09_COMPLIANCE.md` | High-reliability compliance: dual extraction, reconciliation, evidence, deterministic validators, clauses, red-team, amendment revalidation. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 7, 8 |
| 11 | `PHASE_10_COLLABORATIVE_REVIEW.md` | Multi-user review workspace, AI comment validation, quorum policies, consolidated review, approval gate. | Phase tests + all acceptance criteria pass; dependencies: 0, 7, 8, 9 |
| 12 | `PHASE_11_PROPOSAL_SUBMISSION.md` | Post-approval proposal generation, coverage validation, submission package generation, red-team and final approval. | Phase tests + all acceptance criteria pass; dependencies: 0, 7, 8, 9, 10 |
| 13 | `PHASE_12_MCP.md` | Safe MCP interface over implemented capabilities. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 5, 6, 7, 8, 9, 10, 11 |
| 14 | `PHASE_13_SEMANTIC_SEARCH.md` | Embeddings, semantic matches, win-profile recommendations, similarity search. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 7 |
| 15 | `PHASE_14_WEB_UI.md` | Integrated collaborative web UI across the entire working lifecycle. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 5, 6, 7, 8, 9, 10, 11 |
| 16 | `PHASE_15_OUTCOME_LEARNING.md` | Win/loss/no-bid outcomes, learning analytics, evidence-constrained outcome classification. | Phase tests + all acceptance criteria pass; dependencies: 0, 8, 10, 11, 14 |
| 17 | `PHASE_16_STATE_LOCAL.md` **(optional/deferable)** | Optional state/local source adapters, beginning with Texas only when needed. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 14 |
| 18 | `PHASE_17_SCHEDULING_OPS.md` | Scheduling, job chains, observability, status and operational health. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 3, 4, 5, 7, 13 |
| 19 | `PHASE_18_SECURITY_HARDENING.md` | Data classification, secrets, AI gateway policy, localhost/private-host defaults, security hardening. | Phase tests + all acceptance criteria pass; dependencies: 0, 7, 8, 9, 10, 11, 14 |
| 20 | `PHASE_19_TESTING_RELEASE_GATES.md` | Full automated testing, prompt/JEV/compliance regression suites, smoke tests, release gates. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18 |
| 21 | `PHASE_20_FINAL_INTEGRATION_ACCEPTANCE.md` | Feature parity review, definition of done, final integration, v1 release readiness. | Phase tests + all acceptance criteria pass; dependencies: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19 |

## Recommended real-world checkpoints

### Checkpoint A — after Phase 6
Verify that live federal data, search, historical awards, and vendor intelligence are genuinely useful before investing further.

### Checkpoint B — after Phase 8
Run representative real solicitations through AI analysis and JEV. Verify structured outputs, source evidence, and decision routing.

### Checkpoint C — after Phase 9
Manually benchmark compliance on a growing set of real solicitations. Mandatory/critical requirement recall and false-SATISFIED behavior are release-critical.

### Checkpoint D — after Phase 10
Have both real users work on the same opportunity concurrently. Test single-review, dual-review, conditional quorum, disagreement, reopen, and override cases.

### Checkpoint E — after Phase 11
Generate full proposal/submission packages from representative approved bids and manually inspect usability before integrating more surfaces.

### Checkpoint F — after Phase 20
Run the definition-of-done checklist, full regression suites, migration-from-empty-db test, smoke test, and end-to-end real-data walkthrough.

## Standard agent invocation

Use `AGENT_PHASE_PROMPT.md` with the current phase filename substituted.

## Optional phase rule

`PHASE_16_STATE_LOCAL.md` may be deferred until after the federal v1 is complete. If deferred, record it as `DEFERRED (OPTIONAL)` in `IMPLEMENTATION_STATUS.md`; do not treat that as a failed federal-v1 acceptance criterion unless state/local support is explicitly required for the release.
