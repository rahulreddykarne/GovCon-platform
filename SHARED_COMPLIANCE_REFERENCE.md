# Shared Compliance Reference

## 15. Phase 9 — High-reliability compliance subsystem

**Goal:** make compliance a separate, evidence-backed, testable subsystem that automatically identifies mandatory requirements, verifies them against evidence, detects conflicts/amendments, and blocks unsafe submission states.

The compliance system should minimize human work while optimizing for **mandatory-requirement recall** and an extremely low false-satisfied rate.

### 15.1 Compliance architecture

```text
ALL SOLICITATION FILES
        ↓
DOCUMENT INVENTORY
        ↓
TEXT / TABLE EXTRACTION
        ↓
┌────────────────────────────┐
│ Independent Extraction A   │
│ Independent Extraction B   │
└──────────────┬─────────────┘
               ↓
     REQUIREMENT RECONCILIATION
               ↓
        SOURCE-BACKED MATRIX
               ↓
┌──────────────┼───────────────┐
↓              ↓               ↓
Deterministic  Clause Library  AI Validator
Validators
└──────────────┬───────────────┘
               ↓
       CONFLICT / AMENDMENT SCAN
               ↓
        COMPLIANCE RED TEAM
               ↓
          JEV RISK ROUTING
               ↓
      Exceptions → Human Review
               ↓
        PROPOSAL COVERAGE CHECK
               ↓
      SUBMISSION PACKAGE CHECK
               ↓
          FINAL PRE-FLIGHT
               ↓
         READY TO SUBMIT
```

---

### 15.2 Document inventory before requirement extraction

Before any compliance conclusion is produced, create a complete document inventory.

Inventory must include:

```text
solicitation
SOW / PWS
attachments
pricing sheets
forms
amendments
Q&A
drawings/specifications
referenced instructions where downloaded
```

For every source:

```text
filename
source URL
SHA-256
download time
snapshot/version
document type
page count when available
text extraction status
table extraction status
OCR-needed flag
```

The system must detect:

```text
missing expected attachments
duplicate files
same filename with different hash
newer amendment
failed extraction
unreadable pages
```

A compliance run is never considered complete if required source material failed ingestion without a visible warning.

---

### 15.3 Separate requirement extraction from compliance judgment

Do not ask one model:

```text
"Read this solicitation and tell me whether we comply."
```

Instead use three distinct layers:

```text
LAYER 1 — REQUIREMENTS
What does the government require?

LAYER 2 — EVIDENCE
What company/supplier/proposal evidence do we actually have?

LAYER 3 — VALIDATION
Does the evidence satisfy the requirement?
```

This separation is mandatory.

Example:

```text
Requirement:
Delivery within 30 calendar days.

Evidence:
Supplier quote states 12-day lead time.
Transit time not documented.

Compliance conclusion:
NEEDS_REVIEW / UNKNOWN
```

Never jump directly from requirement text to `SATISFIED` without evidence.

---

### 15.4 Every requirement must be source-backed

Every extracted requirement should store:

```text
requirement text
requirement type
mandatory flag
severity
source file
page
section
short supporting quote
snapshot/version
extraction pass
confidence
```

Example:

```text
Requirement:
Delivery within 30 days

Source:
Solicitation.pdf

Page:
18

Section:
4.2 Delivery

Supporting text:
<short source excerpt>

Mandatory:
Yes

Severity:
Critical

Extraction confidence:
0.98
```

If source location cannot be established:

```text
status = needs_review
```

Do not treat an uncited AI statement as authoritative.

---

### 15.5 Independent extraction passes

Do not trust one extraction pass to find all mandatory requirements.

Default:

```text
Pass A → DeepSeek structured requirement extraction
Pass B → independent DeepSeek extraction with different prompt/context strategy
```

For high-risk/high-value solicitations, configurable escalation:

```text
Pass A → DeepSeek
Pass B → Claude/GPT/other approved second model
```

The two passes should be independent enough to reduce shared omission risk.

Reconciliation rules:

```text
A + B both identify same requirement
→ independently_confirmed = true

A only
→ preserve requirement
→ flag for reconciliation

B only
→ preserve requirement
→ flag for reconciliation

A and B disagree on mandatory/severity/meaning
→ needs_review
```

Never discard a requirement solely because only one pass found it.

---

### 15.6 Requirement reconciliation engine

The reconciler should:

```text
deduplicate semantically equivalent requirements
preserve source references from all passes
merge evidence
retain disagreements
detect conflicting mandatory flags
detect conflicting deadlines/quantities/instructions
create one canonical requirement record
link duplicates/superseded versions
```

The reconciler must be conservative.

When uncertain:

```text
keep both candidates
mark for review
```

rather than silently merging unrelated requirements.

---

### 15.7 Deterministic compliance validators

Anything that can be checked exactly should be validated by code before asking an LLM.

Examples:

```text
submission deadline not passed
correct deadline timezone
required file exists
required form exists
required signature detected / explicitly confirmed
required amendment acknowledged
proposal page count within limit
required pricing rows populated
all CLINs accounted for
required quantities accounted for
required file types correct
required filenames correct
file size within limit
all mandatory attachments present
SAM registration state known
set-aside data matches known business facts
delivery date arithmetic
margin arithmetic
```

Each deterministic validator returns:

```json
{
  "status": "pass|fail|unknown",
  "reason": "...",
  "evidence": {},
  "validator_version": "..."
}
```

LLMs must not override a deterministic failure.

---

### 15.8 Clause library

Maintain a local clause/reference library over time.

Initial families may include:

```text
FAR
DFARS
DLA-specific
agency-specific
country-of-origin
packaging
inspection
delivery
cybersecurity
data-handling
representations/certifications
```

Each clause record can store:

```text
clause number
title
category
source
plain-language summary
typical effects
verification questions
expected evidence
default risk level
```

When a solicitation references a known clause:

```text
extract clause
→ match clause library
→ load verification questions
→ create compliance requirements/findings
```

The clause library assists identification and routing. It does **not** replace legal interpretation.

Unknown or materially modified clauses should be surfaced for review.

---

### 15.9 Contradiction detection

The compliance subsystem must search for conflicting instructions across:

```text
solicitation
SOW/PWS
attachments
pricing sheet
amendments
Q&A
submission instructions
```

Example:

```text
Base solicitation:
Delivery = 30 days

Amendment 0002:
Delivery = 20 days
```

System result:

```text
CONFLICT / SUPERSESSION DETECTED

Old requirement:
30 days

Latest controlling candidate:
20 days

Source:
Amendment 0002
```

Version/date logic should determine recency. AI may identify semantic conflict, but source-version rules determine which document is newer.

If controlling precedence is ambiguous:

```text
needs_review
```

---

### 15.10 Amendment-driven invalidation

A material amendment must never simply be appended to the file list.

Workflow:

```text
New amendment
        ↓
Snapshot + diff
        ↓
Which requirements changed?
        ↓
Mark affected requirement conclusions STALE
        ↓
Mark affected proposal sections STALE
        ↓
Re-run sourcing/pricing if affected
        ↓
Re-run compliance validation
        ↓
Re-run JEV bid decision if material
        ↓
Reopen human review if policy requires
```

Possible visible alert:

```text
COMPLIANCE STATUS CHANGED

3 previously satisfied requirements require revalidation
because Amendment 0003 changed relevant source material.
```

No stale requirement should remain silently green.

---

### 15.11 Explicit compliance states

Allowed compliance states:

```text
SATISFIED
MISSING
UNKNOWN
NEEDS_REVIEW
NOT_APPLICABLE
STALE
SUPERSEDED
```

Never collapse:

```text
UNKNOWN → MISSING
UNKNOWN → SATISFIED
```

Example:

```text
Requirement:
Product must comply with XYZ.

Supplier evidence:
Not yet received.

Correct status:
UNKNOWN

Incorrect status:
SATISFIED
```

---

### 15.12 Severity and submission blockers

Severity:

```text
CRITICAL
HIGH
MEDIUM
LOW
```

Typical interpretation:

```text
CRITICAL
Potentially makes the bid non-responsive, impossible to submit,
legally/certification-sensitive, or materially invalid.

HIGH
Serious issue requiring resolution before normal progression.

MEDIUM
Potential issue that may require clarification.

LOW
Formatting/informational/minor concern.
```

JEV may answer:

```text
Should this block submission?
Should this require second-model review?
Should this require human review?
Should bid viability be reassessed?
```

Critical requirements should receive redundant validation even when confidence is high.

---

### 15.13 Compliance coverage scoring

Do not display only one opaque AI percentage.

Show actual counts.

Example:

```text
Mandatory requirements:       47
Satisfied:                    43
Missing:                       2
Unknown:                       1
Needs review:                  1

Critical requirements:        12
Critical satisfied:           11
Critical unresolved:           1

Submission blockers:           2
```

Break down by category:

```text
Administrative    10/10
Technical         12/12
Pricing             8/9
Delivery            5/5
Certifications      4/5
Submission          4/6
```

Any percentage shown must be derived from these counts and clearly defined.

---

### 15.14 Evidence model

Every `SATISFIED` conclusion should identify its supporting evidence.

Possible evidence:

```text
company registration record
supplier quote
supplier specification sheet
historical company record
proposal section
signed form
pricing workbook
deterministic validator output
human verification
```

Requirement-to-evidence example:

```text
Requirement #38
Provide three past-performance references.

Evidence:
Proposal v5, Section 6
Reference A
Reference B
Reference C

Status:
SATISFIED
```

If only two are present:

```text
Status:
MISSING

Severity:
CRITICAL/HIGH according to source requirement
```

---

### 15.15 Compliance red-team agent

After the initial matrix is built, run a separate adversarial review.

Prompt objective:

```text
Assume this bid will be rejected as non-responsive.
Find every plausible source-backed reason why.
```

Search specifically for:

```text
missed attachment
unsigned form
unacknowledged amendment
wrong pricing template
unanswered requirement
unsupported claim
delivery mismatch
country-of-origin issue
wrong file format
page-limit violation
missing certification
incorrect recipient
incorrect submission address
timezone mistake
missing CLIN
missing quantity
stale requirement
conflicting instruction
```

The red-team agent should not reuse the exact extraction prompt.

All findings are stored in `compliance_findings`.

---

### 15.16 Confidence thresholds and escalation

Confidence is a routing signal, not proof.

Initial configurable policy example:

```text
High confidence
+
deterministic validators pass
+
non-critical requirement
→ may auto-accept

Medium confidence
→ independent second AI validation

Low confidence
→ human review

Critical requirement
→ redundant validation regardless of confidence
```

Do not permanently hardcode numerical thresholds before calibration.

If numeric thresholds are later used, keep them in configuration.

---

### 15.17 Proposal-to-requirement coverage validation

After proposal generation, automatically map each response requirement to the selected proposal version.

For every requirement:

```text
Where is it answered?
Which section?
Which page?
What evidence supports it?
Does the answer actually address the requirement?
```

Example:

```text
Requirement #38:
Provide 3 past-performance references.

Proposal:
Section 5, pages 14–16

Detected:
3 references

Result:
SATISFIED
```

If the requirement cannot be located in the final proposal:

```text
BLOCKING FINDING
```

Do not rely solely on the writer model's claim that it covered everything.

---

### 15.18 Final submission-package pre-flight

Compliance validation continues through the actual submission package.

Pre-flight must check:

```text
proposal content
pricing workbook
signed documents
representations
certifications
amendment acknowledgments
required attachments
filenames
file types
file sizes
recipient
portal / email destination
deadline
timezone
submission instructions
```

Think of this as a pre-flight checklist.

Everything machine-checkable must be green before:

```text
READY TO SUBMIT
```

If an item cannot be verified:

```text
UNKNOWN / NEEDS_REVIEW
```

not green.

---

### 15.19 Compliance metrics

Track reliability separately from generic AI quality.

Target metrics:

```text
Mandatory requirement recall
Target: >99% after calibration on representative contracts

Critical requirement recall
Target: as close to 100% as practically achievable

False-satisfied rate
Target: extremely low / near zero

Source-citation accuracy
Target: >99%

Amendment-change detection
Target: >99%

Submission-package completeness
Target: 100% for deterministic checks

Unsupported proposal claims
Target: near zero
```

The most dangerous failure is:

```text
Requirement is actually unresolved
but system reports SATISFIED
```

Therefore optimize the system to prefer:

```text
NEEDS_REVIEW
```

over false certainty.

---

### 15.20 Compliance learning and regression dataset

Every discovered compliance miss becomes a permanent regression case.

Example:

```text
Missed:
Packaging requirement embedded inside Attachment 7 table.

Cause:
Table extraction failed.

Correction:
Add table-aware extraction fallback.

Regression:
Future releases must detect this requirement.
```

Maintain:

```text
tests/fixtures/compliance/
```

Each benchmark case should include:

```json
{
  "source_files": [],
  "expected_mandatory_requirements": [],
  "expected_critical_requirements": [],
  "expected_conflicts": [],
  "expected_amendment_changes": [],
  "expected_submission_files": []
}
```

When prompts/models/parsers change:

```text
run the entire historical compliance benchmark
compare recall
compare false-satisfied rate
compare citation accuracy
block deployment if critical metrics regress
```

---

### 15.21 Compliance UI

The review workspace must show:

```text
Requirement
Mandatory?
Severity
Source
Evidence
Status
Confidence
Independent confirmation?
Amendment freshness
Blocking?
```

Reviewer must be able to open the exact source page/section when available.

Filters:

```text
Critical only
Missing
Unknown
Needs review
Stale
Submission blockers
Recently changed by amendment
Not independently confirmed
```

---

### 15.22 Acceptance criteria

Before the compliance subsystem is considered complete:

1. every requirement has source attribution when source location is available
2. independent extraction passes can be run and reconciled
3. a requirement found by only one pass is never silently discarded
4. deterministic validators exist for machine-checkable rules
5. deterministic failures cannot be overridden silently by LLM output
6. unknown remains distinct from missing and satisfied
7. clause references can map to the clause library
8. conflicting source instructions are surfaced
9. amendments invalidate affected stale conclusions
10. critical requirements receive redundant validation
11. compliance coverage counts are visible by category
12. every satisfied requirement has supporting evidence or validator output
13. red-team findings are persisted
14. proposal-to-requirement coverage is checked automatically
15. final submission package receives a pre-flight validation
16. mandatory/critical unresolved blockers prevent `ready_to_submit`
17. authorized human overrides require explicit reason and audit history
18. compliance benchmark/regression tests run in CI/local release checks
19. source-citation accuracy and requirement recall are measurable
20. a known regression in critical requirement recall blocks release


## Appendix E — Compliance reliability doctrine

The platform should treat compliance similarly to a safety-critical pre-flight process.

### Design priority

```text
1. Do not miss mandatory requirements.
2. Do not falsely mark unresolved requirements as satisfied.
3. Preserve exact source evidence.
4. Use deterministic validation whenever possible.
5. Revalidate after amendments.
6. Escalate ambiguity instead of inventing certainty.
7. Validate the final proposal against the matrix.
8. Validate the final submission package against the instructions.
```

### Preferred failure mode

Preferred:

```text
NEEDS_REVIEW
```

Not preferred:

```text
FALSELY SATISFIED
```

A small amount of extra review noise is acceptable if it materially reduces the chance of silently missing a mandatory requirement.

### Operational target

The architecture should be capable of reaching very high routine compliance reliability while recognizing that unusual solicitation language, scanned files, agency-specific instructions, ambiguous clauses, and legal interpretations may still require human review.

