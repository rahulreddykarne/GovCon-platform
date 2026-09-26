# SPEC_DEVIATIONS

Record only verified deviations from `MASTER_SPEC_v2.5.md`.

## Template

### DEV-001 — Short title
- Phase:
- Date:
- Spec requirement:
- Verified external/repository reality:
- Decision:
- Reason:
- Impact:
- Follow-up:

## Phase 0

No confirmed behavior deviation was required. Choices that add columns or tables required by the specification prose (timestamps, sessions, audit, notifications, `decision_runs`, optimistic `version`) are recorded in `DECISIONS.md`.

## Phase 1

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. The live Get Opportunities documentation still publishes `https://api.sam.gov/opportunities/v2/search`. Parsing and history choices are recorded in ADR-015, ADR-016, and ADR-017.

On 2026-09-26 this environment had no `SAM_API_KEY`. Unauthenticated and invalid-key requests to the production and alpha search URLs returned an empty HTTP 404 from `istio-envoy`, which does not match the documented error text for a missing or invalid key. The committed fixture is the response example published on the official Get Opportunities page, not a call made with a project API key. The live-pull acceptance check runs only when `SAM_API_KEY` is set.
