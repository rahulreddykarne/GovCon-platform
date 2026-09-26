# GovCon v2.5 — Phase 10 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `16. Phase 10 — Collaborative review, AI comment validation, and approval`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Multi-user review workspace, AI comment validation, quorum policies, consolidated review, approval gate.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 7, 8, 9**.

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

- No forced two-review requirement when quorum policy permits one.
- No auto-approval from AI/JEV.
- No proposal generation until approval gate succeeds.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 16. Phase 10 — Collaborative review, AI comment validation, and approval

**Goal:** move human work to the end of analysis. Reviewers inspect the completed AI decision package in parallel, add judgment/corrections, and approve or reject the pursuit.

### Review prerequisites

A review session should normally open only after these are available:

```text
solicitation analysis
historical awards / pricing analysis
supplier or sourcing analysis when applicable
automated compliance matrix
JEV preliminary bid recommendation
AI decision package
```

### Review workspace

Each assigned reviewer sees the same source-backed decision package and can independently record:

```text
overall recommendation
agreement/disagreement with AI recommendation
pricing concerns
supplier concerns
delivery concerns
compliance concerns
competition concerns
other comments
requested follow-up
```

Reviewers work in parallel.

### AI validation of reviewer comments

Each substantive comment is automatically evaluated by the primary analysis model (DeepSeek by default when data policy permits).

Structured output:

```json
{
  "position": "agree|partially_agree|disagree|insufficient_evidence|needs_human_review",
  "confidence": "low|medium|high",
  "reason": "...",
  "supporting_evidence": [],
  "contradicting_evidence": [],
  "missing_information": [],
  "suggested_action": null
}
```

Rules:

- AI must not rewrite the user's comment.
- AI opinion appears beside the original comment.
- AI must cite evidence from the opportunity/package where possible.
- When evidence is absent, return `insufficient_evidence`.
- A reviewer may respond to or override the AI assessment.
- Reviewer comments are never silently deleted or replaced.

### AI consolidated review

After the required review quorum is satisfied:

1. summarize agreements
2. summarize disagreements
3. identify unresolved questions
4. compare reviewer findings to the AI decision package
5. identify any new material risk
6. record whether review was single, dual, conditional, or override-driven
7. send updated structured state to JEV
8. run final JEV review synthesis

Example consolidated result:

```json
{
  "reviewer_alignment": "partial",
  "shared_concerns": ["delivery"],
  "disagreements": ["supplier lead-time interpretation"],
  "new_material_risks": [],
  "open_questions": ["confirm delivered-by date"],
  "jev_final_recommendation": "review",
  "human_approval_required": true
}
```

### Approval gate

After the configured review quorum is satisfied:

```text
APPROVE TO BID
RETURN FOR REVIEW
NO BID
```

If quorum is not satisfied, the normal approval actions remain blocked unless an authorized override is explicitly used.

Only an authorized approver may set `approved_to_bid`.

Approval context must visibly show:

```text
review policy
required review count
completed review count
who reviewed
who did not review
whether second review was triggered
why it was triggered
whether an override was used
AI/JEV disagreements
open risks
```

### Human-work target

Humans should **not** be manually reconstructing the solicitation analysis. Their responsibility is to:

```text
verify
challenge
comment
approve / reject
```

### Acceptance criteria

- Two users can review the same opportunity in parallel.
- Each user's comments are attributed and timestamped.
- AI opinion is generated for substantive comments.
- AI opinion never overwrites human comments.
- Reviewers can complete independently.
- Approval is unavailable until the configured review quorum is satisfied, unless an authorized override is recorded.
- Consolidated review shows agreements, disagreements, open issues, and evidence.
- `approved_to_bid` requires an authorized human action.


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

### 39.6 `reviewer_comment_validation_v1.md`

```text
ROLE
You are an evidence-based collaborative review assistant.

OBJECTIVE
Evaluate the factual substance of one human review comment against the
current source-backed opportunity state.

INPUTS
- reviewer comment
- AI decision package
- relevant solicitation evidence
- supplier/pricing evidence
- compliance state
- historical award evidence

TASK
Return one position:
AGREE
PARTIALLY_AGREE
DISAGREE
INSUFFICIENT_EVIDENCE
NEEDS_HUMAN_REVIEW

Also return:
- concise reason
- supporting evidence
- contradicting evidence
- missing information
- suggested next action

RULES
- Evaluate the statement, not the person.
- Never rewrite or overwrite the human comment.
- Do not manufacture evidence to support either side.
- If the issue cannot be resolved from available evidence, use
  INSUFFICIENT_EVIDENCE.
- For legal/ambiguous procurement interpretations, use NEEDS_HUMAN_REVIEW.
- Cite source evidence.

OUTPUT
Return only JSON conforming to reviewer_comment_validation.v1.
```

---


### 39.7 `consolidated_review_v1.md`

```text
ROLE
You are a review-synthesis engine.

OBJECTIVE
Combine completed human reviews and AI comment validations into a concise,
traceable decision package for JEV and the final human approval gate.

INPUTS
- original AI decision package
- reviewer recommendations
- reviewer comments
- AI validation for each comment
- current sourcing/pricing/compliance state
- review quorum state

TASK
Identify:
- reviewer agreements
- reviewer disagreements
- disagreements with the AI package
- new material risks
- resolved issues
- unresolved questions
- evidence needed before approval
- whether prior analysis became stale

RULES
- Do not average away a material disagreement.
- If one reviewer says BID and another says NO BID, preserve the split.
- Distinguish reviewer opinion from source-backed fact.
- Never make the final approval decision.

OUTPUT
Return only JSON conforming to consolidated_review.v1.
```

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

**Do not begin Phase 11 in the same implementation run.**
