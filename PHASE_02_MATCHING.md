# GovCon v2.5 — Phase 2 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `8. Phase 2 — Watchlist matching engine`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Watchlists, deterministic matching, explainable shortlist generation.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1**.

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

- No semantic/vector matching yet.
- No AI bid decisions.
- No proposal workflow.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 8. Phase 2 — Watchlist matching engine

**Goal:** reduce the federal firehose to a useful shortlist.

### Matching semantics

Every non-empty rule group must pass. Values inside a group are OR'd.

Rules:

- PSC prefix matching
- NAICS prefix matching
- keywords
- exclude keywords
- exact NSN list
- set-asides
- source filters
- min/max opportunity value when known
- minimum days until deadline

Unknown values do not get fabricated.

Example:

- if `max_value` is configured but opportunity value is unknown, do not silently reject.
- record the value filter as `unknown` in matching evidence.

### Score

Initial score may be rule-hit based, but must store explainable evidence in `matched_on`.

### CLI

```text
govcon match run
govcon match rebuild --watchlist N
govcon watchlist add
govcon watchlist list
govcon watchlist edit
govcon watchlist disable
```

### Acceptance criteria

Tests cover:

- PSC prefix
- NAICS prefix
- exclude veto
- wildcard empty group
- unknown estimated value
- idempotent match upsert

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

**Do not begin Phase 3 in the same implementation run.**
