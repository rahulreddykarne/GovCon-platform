# GovCon v2.5 — Phase 11 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `17. Phase 11 — Post-approval proposal and submission-package generation`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Post-approval proposal generation, coverage validation, submission package generation, red-team and final approval.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 7, 8, 9, 10**.

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

- No arbitrary portal auto-submission.
- No invented company/supplier facts.
- No final submission without authorized human approval.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 17. Phase 11 — Post-approval proposal and submission-package generation

**Goal:** once the collaborative review is complete and the opportunity is approved to bid, generate the proposal and submission materials automatically with minimal additional human effort.

### Trigger

This phase begins only when:

```text
review_session.final_approval_status = approved_to_bid
```

### AI proposal generation

Generate the response structure from the verified compliance matrix rather than from a fixed universal template.

Possible sections:

```text
Cover / Quote Letter
Executive Summary
Technical / Product Response
Delivery Plan
Past Performance
Management / Quality
Pricing Narrative
Representations / Certifications
Required Forms
Attachments
```

Rules:

1. Use only verified source data, approved company facts, approved reusable content, supplier evidence, and reviewed assumptions.
2. Never invent past performance, certifications, supplier commitments, delivery dates, product specs, pricing, or registrations.
3. Unknown fields remain explicit placeholders / blockers.
4. Every material proposal section maps back to requirement IDs.
5. Every regeneration creates a new immutable proposal version.

### AI submission-package generation

Automatically generate a structured submission package:

```text
submission method
submission destination
recipient / portal
deadline + timezone
required filenames
required forms
signature requirements
amendment acknowledgments
required attachments
file-size limits
email subject/body when applicable
portal/manual submission instructions
final checklist
```

The system should also generate:

```text
cover letter
submission email draft
file naming plan
final ZIP/package manifest
missing-document list
step-by-step submission instructions
```

### AI red-team and compliance validation

Before the package reaches final approval, invoke the dedicated compliance subsystem rather than a generic single-model review.

Required checks:

```text
proposal-to-requirement mapping
proposal completeness review
unsupported-claim detection
mandatory requirement coverage
critical requirement coverage
format/page-limit checks
pricing consistency
amendment acknowledgment checks
stale-requirement check
contradiction check
file/package completeness
submission instruction validation
final deterministic pre-flight
```

The proposal writer's own assertion that a requirement is covered is never sufficient.

Use DeepSeek as the primary reviewer when policy permits. JEV scores/routs issues by severity and determines whether another model or human review is needed.

### Minimal final human step

The final approver should see:

```text
READY / NOT READY
mandatory requirement counts
critical requirement counts
blocking issues
unknown / needs-review items
stale items
final proposal version
requirement-to-proposal coverage
required attachments
submission destination
deadline + timezone
generated instructions
deterministic pre-flight result
AI/JEV final validation
```

Actions:

```text
APPROVE FOR SUBMISSION
RETURN FOR FIX
CANCEL BID
```

The user should not need to manually assemble the package if the system has sufficient source data.

### v1 submission policy

The platform may:

- assemble required files
- generate final proposal documents
- generate submission instructions
- draft submission email
- validate readiness
- mark ready
- record final approval
- record confirmation after submission

The platform must **not** autonomously click through or submit into arbitrary government portals in v1.

### Acceptance criteria

- Approval-to-bid automatically starts proposal/package generation.
- Proposal sections are traceable to compliance requirements.
- Submission instructions are generated from solicitation evidence.
- Missing mandatory items block `ready`.
- Final package can be exported as DOCX/PDF/XLSX/ZIP as applicable.
- Final approval remains human-authorized.
- No arbitrary portal auto-submission occurs in v1.


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

## 41. Production starter prompts — proposal generation & review

### 41.1 `proposal_drafting_v1.md`

```text
ROLE
You are a government-contract proposal drafting engine.

OBJECTIVE
Draft the requested proposal section(s) using only approved, source-backed
facts and the verified compliance matrix.

ALLOWED INPUT FACTS
1. verified solicitation requirements
2. approved company facts
3. approved past-performance records
4. verified supplier/product evidence
5. approved pricing
6. approved reviewer assumptions/decisions
7. approved reusable company content

NEVER INVENT
- certifications
- registrations
- contracts previously performed
- customer references
- staff qualifications
- product specifications
- supplier commitments
- inventory availability
- delivery commitments
- prices
- signatures

TASK
For each section:
- answer its mapped requirement IDs
- use concise, responsive language
- preserve required terminology
- avoid unsupported marketing claims
- identify blockers instead of filling missing facts
- return source/fact IDs used to support material claims

PLACEHOLDER FORMAT
If required information is missing, insert:
[[BLOCKER:<short description>]]

OUTPUT
Return only JSON conforming to proposal_draft.v1, containing structured
sections plus blocker list and supporting fact references.
```

---

### 41.2 `proposal_red_team_v1.md`

```text
ROLE
You are a skeptical proposal red-team reviewer.

OBJECTIVE
Find weaknesses in the selected proposal version before submission.

INPUTS
- solicitation requirements
- compliance matrix
- proposal version
- approved company facts
- supplier/pricing evidence

FIND
- weak or incomplete answers
- unsupported claims
- contradictions
- vague promises
- missing requirement coverage
- inconsistent dates/quantities/prices
- statements stronger than available evidence
- unnecessary content that risks page limits
- proposal language inconsistent with the solicitation

CLASSIFY EACH FINDING
critical | major | minor

RULES
- Do not invent weaknesses.
- Tie each finding to evidence.
- Do not rewrite the full proposal unless explicitly requested.
- Identify which requirement/section is affected.

OUTPUT
Return only JSON conforming to proposal_red_team.v1.
```

---

### 40.7 `proposal_coverage_v1.md`

```text
ROLE
You are a requirement-to-proposal coverage auditor.

OBJECTIVE
Verify that every response-required solicitation requirement is actually
addressed in the selected final proposal version.

INPUTS
- canonical compliance matrix
- proposal text/sections
- deterministic page/format results

FOR EACH REQUIREMENT RETURN
- requirement_id
- coverage_status:
  COVERED | PARTIAL | NOT_FOUND | NEEDS_REVIEW
- proposal_section
- proposal_page when available
- supporting proposal excerpt
- source requirement reference
- issue description

RULES
- Do not trust section titles alone.
- Verify substantive coverage.
- Do not mark a requirement COVERED because the drafting model says it covered it.
- Requirements calling for external forms/attachments should reference those
  artifacts rather than pretending prose satisfies them.
- Critical NOT_FOUND findings are blockers.

OUTPUT
Return only JSON conforming to proposal_coverage.v1.
```

---


### 40.8 `submission_preflight_ai_v1.md`

```text
ROLE
You are the AI component of a final government-bid submission pre-flight.

OBJECTIVE
Review the assembled final package for source-backed issues that deterministic
validators may not fully understand.

INPUTS
- final compliance matrix
- deterministic pre-flight results
- final proposal
- file manifest
- submission instructions
- amendment list
- destination/recipient data

CHECK
- package appears consistent with submission instructions
- narrative and attachments do not contradict each other
- expected forms appear semantically appropriate
- amendment acknowledgments correspond to known amendments
- proposal references the correct solicitation where relevant
- unresolved UNKNOWN / NEEDS_REVIEW states are visible
- no stale compliance conclusions remain

RULES
- Deterministic failures remain failures.
- Do not declare READY if a critical blocker exists.
- Do not fabricate signatures, files, or acknowledgments.
- Return unresolved ambiguity explicitly.

OUTPUT
Return only JSON conforming to submission_preflight_ai.v1.
```

---

## Appendix B — Recommended proposal review sequence

```text
1. Source extraction complete
2. Compliance matrix reviewed
3. Bid approved
4. Pricing/supplier evidence entered
5. Proposal v1 generated
6. Compliance reviewer checks against solicitation
7. Red-team reviewer finds weaknesses
8. User edits
9. New immutable version created
10. Final consistency review
11. Submission checklist generated
12. Human marks ready
13. Human submits
14. Confirmation recorded
```

---

## Appendix C — Future automated submission architecture

Do not implement in v1.

If implemented later:

```text
SubmissionManager
├── EmailSubmissionAdapter
├── PIEEAdapter
├── EBuyAdapter
├── AgencyPortalAdapter
└── ManualAdapter
```

Each adapter must provide:

```text
validate()
prepare()
preview()
submit()        # requires explicit human confirmation
confirm()
status()
```

Mandatory safeguards:

- duplicate submission prevention
- deadline/timezone validation
- final file hash capture
- confirmation receipt capture
- no DB-stored portal password
- explicit user confirmation immediately before submit
- detailed audit log
- adapter disabled by default until tested with non-production/safe workflow

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

**Do not begin Phase 12 in the same implementation run.**
