# GovCon v2.5 — Phase 4 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `10. Phase 4 — DIBBS ingestion`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

DIBBS verification, ingestion, NSN/quantity normalization, idempotency.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 2**.

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

- No arbitrary per-solicitation crawling unless verified/required by the master spec.
- No pricing decisions.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 10. Phase 4 — DIBBS ingestion

**Goal:** ingest DLA commodity solicitations with strong NSN/quantity coverage.

### ⚠️ VERIFY FIRST

Before coding:

1. inspect live DIBBS pages/files
2. identify currently available batch/export mechanism
3. save one real daily fixture
4. document file layout
5. check access terms and robots behavior
6. use conservative sequential request rate

### Tasks

- download source batch files
- retain original batch files
- parse solicitation number
- NSN
- nomenclature
- quantity
- unit
- return-by date
- set-aside
- buyer details
- source URL
- amendments/cancellations through upsert + snapshots

Do not scrape individual HTML pages in v1 unless required and explicitly documented.

### Acceptance criteria

- Today's fixture ingests.
- NSN/quantity coverage is measured and logged.
- Re-run is idempotent.
- DIBBS rows work with the same watchlist engine as SAM rows.

---


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

**Do not begin Phase 5 in the same implementation run.**
