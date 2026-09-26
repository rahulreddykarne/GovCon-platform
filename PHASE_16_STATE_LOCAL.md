# GovCon v2.5 — Phase 16 Build Spec (OPTIONAL / DEFERABLE)

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `22. Phase 16 — State & local adapters`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

**Optional / deferable for federal v1.** State/local source adapters, beginning with Texas only when there is a real business need.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 2, 14**.

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

- No nationwide scraping.
- No source adapter without live verification and a real need.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 22. Phase 16 — State & local adapters

**Goal:** expand only after federal workflow is stable.

Start with Texas when the user is ready.

### Adapter contract

```python
fetch() -> list[NormalizedOpportunity]
```

Each adapter handles:

- discovery
- source-specific auth
- raw storage
- normalization
- source links
- attachment discovery
- change detection

### ⚠️ VERIFY

Before building a state adapter:

- look for official API
- data export
- RSS/feed
- structured downloads
- public search behavior
- terms/robots

Prefer official structured access over scraping.

### Rule

Do not attempt nationwide state/local coverage in v1.

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

**Do not begin Phase 17 in the same implementation run.**
