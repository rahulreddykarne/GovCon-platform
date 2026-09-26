# GovCon v2.5 — Phase 14 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `20. Phase 14 — Web UI`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Integrated collaborative web UI across the entire working lifecycle.

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

- No duplicate business logic in routes/templates; use services.
- No anonymous app access.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 20. Phase 14 — Web UI

**Goal:** complete the entire AI-first bid lifecycle from one shared collaborative web application.

Serve at:

```text
http://localhost:8000
```

### Pages

#### 1. Inbox `/`

Show new matches grouped by watchlist.

Actions:

```text
Seen
Dismiss
Review
Pursue
```

#### 2. Search `/search`

Full-text + filters across all opportunities.

#### 3. Opportunity detail `/opp/{id}`

Show:

- current normalized data
- original source link
- timeline
- amendment/snapshot history
- contacts
- attachments
- AI summary
- historical awards
- vendors/competitors
- bid analysis

#### 4. Workspace `/workspace/{id}`

This is the primary bid-working screen.

Tabs:

```text
Overview
AI Decision Package
Requirements
Market Intelligence
Historical Awards
Products & Suppliers
Pricing
Competitors
Compliance
Collaborative Review
Proposal
Submission
Activity
```

Before review approval, `Proposal` and `Submission` show planned/generated-later state. After `APPROVE TO BID`, those tabs populate automatically from Phase 11.

#### 5. Pipeline `/pipeline`

Columns:

```text
Ingested
AI Analyzing
Sourcing / Pricing
Compliance
Ready for Review
Collaborative Review
Approval Pending
Approved to Bid
Generating Proposal
Submission Validation
Ready to Submit
Submitted
Won
Lost
No Bid
```

Show value/margin/deadline.

#### 6. Watchlists `/watchlists`

CRUD + rebuild.

#### 7. Vendors `/vendors`

Search + historical win profile.

#### 8. Runs `/ops`

Ingestion/job health.

#### 9. Learning `/learning`

Show outcome analytics:

- bids submitted
- wins/losses
- no-bid reasons
- margins
- agencies
- PSCs
- suppliers
- repeat competitors
- common compliance issues
- average lead time
- common loss reasons

### UI behavior

- server-rendered
- HTMX
- one CSS file
- no JS framework/build tool in v1
- clear deadline urgency
- clear AI vs human labels
- clear unknown/missing states
- source citations clickable where possible

### Acceptance criteria

A full workflow must be possible entirely in the UI:

```text
discover
→ AI analyze
→ source/price automatically
→ build compliance automatically
→ generate AI decision package
→ collaborative human review
→ approve bid
→ auto-generate proposal
→ auto-generate submission package
→ AI validate
→ final human submit approval
→ record submitted
→ mark won/lost
→ record lessons learned
```

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

**Do not begin Phase 15 in the same implementation run.**
