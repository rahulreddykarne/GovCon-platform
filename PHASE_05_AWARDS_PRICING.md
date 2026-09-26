# GovCon v2.5 — Phase 5 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `11. Phase 5 — USAspending awards + pricing intelligence`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

USAspending award history and pricing intelligence.

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

- No fabricated unit prices.
- No autonomous bid pricing commitment.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 11. Phase 5 — USAspending awards + pricing intelligence

**Goal:** know what the government paid and who won before pricing a bid.

### API

USAspending.gov.

⚠️ VERIFY current endpoint names, award fields, filters, and pagination before coding.

### Tasks

1. Pull contract award history for PSC/NAICS areas represented by enabled watchlists.
2. Default lookback: 3 years.
3. Use incremental updates after initial backfill.
4. Store raw payloads.
5. Extract NSN from descriptions only when clearly present.
6. Derive quantity/unit price only when supported by source data.
7. Never derive fake unit prices from total obligation alone.
8. Implement:

```text
price_history(nsn)
price_history_psc(psc, keywords)
award_history_for_agency(...)
top_awardees(...)
```

9. Create recompete heuristic view from older awards.

### Acceptance criteria

- Known NSN can show award history.
- Pricing history returns vendor/date/amount and unit price only when actually known.
- Digest can display recent award comps.

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

**Do not begin Phase 6 in the same implementation run.**
