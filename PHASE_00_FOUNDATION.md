# GovCon v2.5 — Phase 0 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `6. Phase 0 — Scaffolding`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Repository, database, configuration, authentication baseline, auditability, prompt/JEV-ready project structure.

## Dependencies

No prior implementation phase is required.

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

- No live SAM/DIBBS/USAspending ingestion.
- No contract analysis.
- No JEV decisions.
- No proposal generation.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 6. Phase 0 — Scaffolding

**Goal:** empty but runnable project.

### Tasks

1. Initialize repo and `pyproject.toml`.
2. Use the repository layout from §3.
3. Create config, DB, HTTP, logging, and CLI foundations.
4. Wire Alembic.
5. Initial migrations may be split logically, but the schema in §5 must be represented.
6. Add `docker-compose.yml` for PostgreSQL 16 + pgvector.
7. Add `govcon db upgrade`.
8. Add `govcon db seed-demo-watchlist`.
9. Add `govcon status`.
10. Add pytest config.
11. Add `.gitignore` covering `.env`, downloaded data, logs, exports, and local credentials.

### Acceptance criteria

- `docker compose up -d && govcon db upgrade` succeeds.
- `pytest` succeeds.
- `govcon --help` shows command groups.
- `.env` is never committed.
- `govcon status` prints DB connectivity and schema revision.

---


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

## 24A. Collaboration, authentication, and concurrency

**Goal:** support two or more trusted reviewers working on the same opportunity safely.

### Authentication

The web app is no longer anonymous.

Minimum requirements:

```text
invite-only user creation
secure password hashing
session-based authentication
logout
inactive-user disable
basic role checks
```

Initial roles:

```text
owner
approver
reviewer
read_only
```

A single user may have multiple roles if implemented as permissions later.

### Review ownership

Every review/comment/action stores:

```text
user_id
timestamp
opportunity_id
action type
old/new state when applicable
```

### Concurrent work

Use optimistic concurrency/version checks for shared mutable records.

Do not use last-write-wins for:

```text
final assessment
proposal versions
approval status
submission status
```

Comments should be append-first and threaded.

### Notifications

Initial implementation may use in-app notifications and email alerts for:

```text
review assigned
reviewer completed
new reviewer comment
AI flagged comment as needing evidence
review quorum satisfied
second review required
second review requested
review reassigned
approval pending
material amendment after review
proposal package generated
submission ready
```

### Acceptance criteria

- Two logged-in users can open the same opportunity simultaneously.
- Neither user silently overwrites the other's work.
- Reviewer identity is visible on comments and completion state.
- A one-review quorum can progress without waiting for an optional second reviewer.
- Mandatory two-review quorum blocks progression until satisfied or explicitly overridden.
- Conditional second-review triggers are configurable and auditable.
- Reviewer-requested second review is honored.
- Approval permissions are enforced.
- Audit history shows who changed what and when.

---

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

**Do not begin Phase 1 in the same implementation run.**
