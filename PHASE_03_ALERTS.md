# GovCon v2.5 — Phase 3 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `9. Phase 3 — Alert digests`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Alert digest and amendment re-alert behavior.

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

- No complex notification center.
- No bid/proposal notifications beyond defined digest behavior.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 9. Phase 3 — Alert digests

**Goal:** surface new matches without requiring the UI.

### Tasks

1. Collect unalerted `new` matches.
2. Group by watchlist.
3. Render HTML digest.
4. Include:
   - title
   - agency
   - source
   - PSC/NAICS
   - set-aside
   - deadline + days remaining
   - estimated value when known
   - direct source link
5. Later phases enrich the digest with:
   - historical awards
   - likely competitors
   - bid recommendation status
6. SMTP if configured.
7. Otherwise write to `OUTBOX_DIR`.
8. Never re-alert the same match unless a material amendment creates a configured re-alert condition.

### Acceptance criteria

- Empty day = no message.
- Repeat run = no duplicate alerts.
- Material deadline change can optionally generate an amendment alert.

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

**Do not begin Phase 4 in the same implementation run.**
