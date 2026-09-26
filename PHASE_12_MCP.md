# GovCon v2.5 — Phase 12 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `18. Phase 12 — MCP server`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Safe MCP interface over implemented capabilities.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 2, 5, 6, 7, 8, 9, 10, 11**.

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

- No new business logic unique to MCP; expose existing services.
- No secret leakage through MCP.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 18. Phase 12 — MCP server

**Goal:** let Claude or another MCP-capable assistant operate the local GovCon system safely.

### Read tools

Stable MCP contracts:

```text
search_opportunities(...)
get_opportunity(id)
get_opportunity_history(id)
price_history(...)
vendor_profile(...)
competitor_summary(...)
list_matches(...)
pipeline_summary(...)
get_bid_analysis(opportunity_id)
get_compliance_matrix(opportunity_id)
get_proposal(opportunity_id)
submission_status(opportunity_id)
learning_summary(...)
similar_opportunities(...)
```

### Write tools

```text
update_match(...)
add_pursuit(...)
update_pursuit(...)
record_human_bid_decision(...)
assign_reviewer(...)
add_review_comment(...)
complete_review(...)
request_ai_comment_validation(...)
approve_to_bid(...)
update_requirement_status(...)
create_proposal_version(...)
set_submission_ready(...)
record_submission_confirmation(...)
record_outcome(...)
```

### Safety rules

- Read tools return compact structured output.
- Descriptions are truncated unless explicitly requested.
- Write tools echo changed record.
- Submission tools never auto-submit.
- Destructive operations require explicit intent.
- Errors return structured messages, not stack traces.
- MCP never exposes secrets.

### Acceptance criteria

Example end-to-end interaction:

> Show new matches closing in the next 7 days, include historical prices, explain the top bid candidate, move it to reviewing, and show what information is missing.

Must complete through MCP against local DB.

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

**Do not begin Phase 13 in the same implementation run.**
