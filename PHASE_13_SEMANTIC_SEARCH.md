# GovCon v2.5 — Phase 13 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `19. Phase 13 — Semantic search & recommendations`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Embeddings, semantic matches, win-profile recommendations, similarity search.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 2, 7**.

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

- No semantic match may bypass hard eligibility filters.
- No autonomous pursue action from similarity alone.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 19. Phase 13 — Semantic search & recommendations

**Goal:** surface relevant opportunities keyword rules miss.

### Tasks

1. Embed opportunity text locally.
2. Add vector index.
3. Build watchlist profile embeddings.
4. Surface semantic-only recommendations separately.
5. Add win-profile embedding once at least 3 genuine wins exist.
6. Never let semantic similarity bypass hard eligibility filters.
7. Add `similar_opportunities`.

### Recommendation categories

```text
Rule match
Semantic match
Similar to won bids
Similar to pursued bids
Recompete radar
```

Keep them visually distinct.

### Acceptance criteria

- Semantic match works with no keyword overlap.
- Search stays interactive at target scale.
- Semantically similar but ineligible opportunity is flagged, not auto-pursued.

---


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

## 24. Phase 18 — Security & data handling

**Goal:** ensure the local convenience tool does not accidentally become a data-leak mechanism.

### Data classification

At minimum:

```text
PUBLIC
PROPRIETARY
FCI
CUI
SECRET_CREDENTIAL
```

### Default classification

- public solicitation and public award data → `PUBLIC`
- supplier quotes and internal pricing → `PROPRIETARY`
- company strategy/proposal drafts → `PROPRIETARY`
- portal passwords/tokens → `SECRET_CREDENTIAL`
- FCI/CUI only when explicitly identified or contractually applicable

### AI gateway rules

Before an external model call:

1. classify content
2. inspect provider policy config
3. block disallowed classifications
4. log provider/model + classification + purpose
5. never log secret values

Example:

```text
PUBLIC → external approved provider allowed
PROPRIETARY → configurable
FCI → blocked by default
CUI → blocked by default
SECRET_CREDENTIAL → always blocked
```

### Credential rules

Do not store:

```text
SAM password
PIEE password
portal cookies
MFA secrets
browser session tokens
```

in PostgreSQL.

Use environment variables, OS credential storage, or future secret-manager integration.

### Local app exposure

Default bind:

```text
127.0.0.1
```

Do not bind `0.0.0.0` unless user explicitly configures it.

### Acceptance criteria

- secret fields never appear in logs
- AI gateway blocks disallowed content
- app defaults to localhost only
- repository contains no live credentials

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

**Do not begin Phase 14 in the same implementation run.**
