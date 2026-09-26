# GovCon v2.5 — Phase 6 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `12. Phase 6 — Vendors, contacts, and competitor intelligence`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Vendor/entity profiles, buyer contacts, competitor intelligence.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 5**.

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

- No unsupported competitor predictions.
- No CRM/team-selling expansion.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 12. Phase 6 — Vendors, contacts, and competitor intelligence

**Goal:** understand prior winners and buyer contacts.

### SAM entity API

⚠️ VERIFY current endpoint/version and permitted fields.

### Tasks

1. Fetch vendor/entity details lazily.
2. Cache with `fetched_at`.
3. Vendor profile shows:
   - registration status
   - CAGE/UEI
   - business types
   - NAICS
   - award count
   - total obligations
   - top agencies
   - top PSCs
4. Competitor view:
   - top winners for same NSN
   - same PSC
   - same agency
   - same office
5. Buyer contacts are harvested from opportunities.
6. Search contacts by name/agency/email.

### Acceptance criteria

- Vendor lookup returns profile + computed historical award stats.
- Cache prevents unnecessary API calls.
- Opportunity page can show likely historical competitors.

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

**Do not begin Phase 7 in the same implementation run.**
