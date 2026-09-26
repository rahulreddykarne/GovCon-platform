# GovCon v2.5 — Phase 1 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `7. Phase 1 — SAM.gov opportunity ingestion + snapshot history`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

SAM.gov ingestion, immutable snapshots, diffs, contacts, archive handling, source fidelity.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0**.

## Agent execution contract

This phase document is derived from `MASTER_SPEC_v2.5.md`. The master specification remains authoritative.

Before coding:

1. Read `MASTER_SPEC_v2.5.md`.
2. Read this phase document completely.
3. Inspect the current repository before creating files or changing architecture.
4. Confirm all listed phase dependencies are implemented and passing.
5. Identify every `⚠️ VERIFY` item in this phase and verify the current official/live interface before coding.
6. Reuse existing modules and patterns; do not create duplicate infrastructure.
7. Implement **only this phase** and required dependency fixes.
8. Run the phase tests and verify every acceptance criterion individually.
9. Do not proceed to another phase in the same agent run.
10. Record unavoidable deviations in `SPEC_DEVIATIONS.md` and architectural decisions in `DECISIONS.md`.
11. Update `IMPLEMENTATION_STATUS.md` before finishing.

If this phase conflicts with a live external interface, trust the verified live interface and document the deviation rather than silently changing product behavior.


## Explicit phase boundaries — do not build yet

- No DIBBS or USAspending ingestion.
- No AI analysis.
- No proposal/compliance workflow.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 7. Phase 1 — SAM.gov opportunity ingestion + snapshot history

**Goal:** reliable federal opportunity ingestion with immutable observed history.

### API

SAM.gov Get Opportunities Public API.

⚠️ VERIFY before implementation:

- current endpoint/version
- authentication method
- pagination behavior
- parameter names
- rate limits
- attachment fields
- notice/amendment identifiers

Historically expected endpoint:

```text
https://api.sam.gov/opportunities/v2/search
```

### Tasks

1. Implement `ingest/sam_opportunities.py`.
2. Default incremental window: last 3 days.
3. Paginate fully.
4. Handle 429s and transient errors with exponential backoff.
5. Normalize into `opportunities`.
6. Preserve full payload in `raw`.
7. Compute canonical payload/content hash.
8. Insert into `opportunity_snapshots` on first observation or material change.
9. Apply field-diff events before updating the current row.
10. Upsert buyer contacts.
11. Parse NSN candidates from title/description.
12. Parse quantity only when clearly present.
13. Parse estimated values only when source data explicitly supports them.
14. Backfill command chunks into API-safe windows.
15. Archive sweep is a separate no-network task.
16. Save at least one real response fixture.

### Upsert-with-diff fields

At minimum track:

```text
response_deadline
set_aside_code
status
links
title
description hash
quantity
estimated_value_min
estimated_value_max
```

### Acceptance criteria

- Re-running an unchanged fixture inserts zero duplicate opportunities and zero duplicate snapshots.
- A changed payload creates one new snapshot.
- A deadline change creates `deadline_changed`.
- Raw source JSON remains available.
- Live pull yields at least one SAM record when valid API access exists.

---


---

## Database/schema coordination

Before changing schema, inspect the canonical database design in §5 of `MASTER_SPEC_v2.5.md` and the current Alembic history. Do not create parallel/duplicate tables for concepts already represented in the master schema. Use migrations and verify upgrade from an empty database.

---

## Phase completion gate

The agent may mark this phase complete only when:

- all dependencies were confirmed passing before implementation
- all `⚠️ VERIFY` items were verified and documented
- required migrations apply cleanly
- required unit/integration/fixture tests pass
- every acceptance criterion in the canonical phase section is explicitly checked
- no known required item is silently deferred
- `IMPLEMENTATION_STATUS.md` is updated
- any spec deviation is recorded in `SPEC_DEVIATIONS.md`
- any durable architecture choice is recorded in `DECISIONS.md`

**Do not begin Phase 2 in the same implementation run.**
