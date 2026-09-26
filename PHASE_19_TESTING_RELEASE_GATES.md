# GovCon v2.5 — Phase 19 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `25. Phase 19 — Testing strategy`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Full automated testing, prompt/JEV/compliance regression suites, smoke tests, release gates.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18**.

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

- No release with critical compliance/prompt regression.
- No network dependency in normal unit tests.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 25. Phase 19 — Testing strategy

### Parser tests

- captured real payload fixtures
- no live network in normal test suite
- respx/httpx mocks

### Idempotency tests

Every ingestion source:

```text
run fixture once
run fixture again
expect zero duplicate logical records
```

### Snapshot tests

- unchanged payload → zero new snapshot
- changed payload → one new snapshot
- diff event references snapshot

### Matching tests

Table-driven.

### AI schema tests

Use deterministic fixture responses.

Validate:

- malformed JSON rejected
- missing required fields handled
- source refs persisted
- no silent fallback into unstructured text
- prompt name/version/hash persisted
- schema version persisted
- generation settings persisted
- prompt-injection-like text inside source documents cannot alter the system/task contract
- unknown values remain explicit rather than fabricated

### Prompt-library tests

Every active prompt must pass:

```text
template render test
required-variable test
schema compatibility test
prompt hash stability test
forbidden-secret scan
source-security / prompt-injection fixture test
representative golden-set evaluation
```

Safety-critical prompt groups:

```text
requirement extraction
requirement reconciliation
compliance validation
amendment analysis
proposal coverage
submission pre-flight
bid-decision support
```

must additionally pass task-specific regression gates before activation.

CLI:

```text
govcon prompts list
govcon prompts validate
govcon prompts render <prompt_name> --fixture <fixture>
govcon prompts diff <name>@vN <name>@vN+1
govcon prompts eval <prompt_name>@<version>
govcon prompts eval --suite compliance
govcon prompts activate <prompt_name>@<version>
```

`activate` must fail if required regression metrics are below configured thresholds.

### Decision tests

- hard eligibility failure
- incomplete evidence
- human override
- repeat analysis versioning

### Collaborative review / quorum tests

- single-review policy proceeds after one completion
- dual-review policy blocks after only one completion
- conditional policy proceeds with one review when no triggers fire
- conditional policy requires second review when configured risk trigger fires
- reviewer-requested second review becomes mandatory
- approver override requires reason and audit record
- second reviewer can be reassigned
- completed review can be reopened without deleting history
- reviewer disagreement routes to approval gate
- BID vs NO BID split never auto-resolves
- AI/JEV late risk can increase required review count
- AI comment-validation failure preserves human review
- material amendment reopens stale review when policy requires
- optional second reviewer does not block progression

### Compliance tests

- required item extraction
- independent extraction pass reconciliation
- requirement detected by only one pass is retained
- missing mandatory item
- unknown state remains unknown
- source traceability
- source citation accuracy
- deterministic deadline validation
- deterministic page-limit validation
- required-file validation
- required-signature validation
- amendment acknowledgment validation
- CLIN/quantity completeness
- clause-library matching
- conflicting-source detection
- amendment invalidates stale requirement
- critical requirement redundant validation
- evidence required before satisfied state
- compliance red-team issue persistence
- proposal-to-requirement coverage
- final submission pre-flight
- false-satisfied regression detection
- mandatory requirement recall benchmark
- critical requirement recall benchmark
- readiness blocking

### Proposal tests

- new version never overwrites old version
- requirement links preserved
- unsupported claim flagging

### Submission tests

- missing requirement prevents ready state
- explicit override is logged
- submitted timestamp/confirmation recorded
- no automatic portal call exists

### Compliance release gate

A build should not be considered releasable when the compliance benchmark shows a material regression in:

```text
critical requirement recall
mandatory requirement recall
false-satisfied rate
source citation accuracy
amendment-change detection
```

Critical-regression failures must block release until explicitly investigated and resolved.

### Migration test

```text
alembic upgrade head
```

against an empty database.

### Smoke test

`scripts/smoke.sh`:

```text
db upgrade
seed demo watchlist
ingest fixtures
snapshot diff
match
digest
validate prompt registry
render and schema-check fixture prompts
analyze fixture opportunity
generate bid recommendation
generate compliance matrix
create proposal v1
prepare submission checklist
assert outputs
```

---


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

## 35. JEV testing and calibration

### Fixture set

Create representative fixtures for:

```text
obvious BID
obvious NO BID
ambiguous REVIEW
deadline-critical opportunity
strong incumbent
poor margin
missing certification
country-of-origin concern
supplier lead-time problem
major amendment
missing mandatory submission item
strong historical price alignment
weak historical comparability
```

### Tests

For each bundle:

- schema validation
- stable allowed output values
- missing-state behavior
- confidence handling
- fallback handling
- hard-rule override
- human-review escalation
- re-run after changed state
- persistence into `decision_runs`

### Calibration dataset

Maintain a local benchmark:

```text
tests/fixtures/decisions/
```

Each case contains:

```json
{
  "state": {},
  "expected_allowed_decisions": [],
  "must_escalate": false,
  "notes": ""
}
```

Do not require exact probabilistic equality; test safety boundaries and acceptable output classes.

---


## 36. JEV acceptance criteria

Before considering the decision layer complete:

1. `JevDecisionProvider` implements the common `DecisionProvider` interface.
2. At least Bundles 1, 2, 4, 5, 6, 7, 8, and 11 are implemented.
3. Every result passes Pydantic schema validation.
4. Every run is stored in `decision_runs`.
5. Changed material state triggers a fresh decision rather than reusing stale output.
6. Hard rules override conflicting JEV recommendations.
7. Low-confidence/high-risk results route to human review.
8. `bid_decision` cannot directly set `bid_approved`.
9. `submission_readiness=ready` cannot directly set `submitted`.
10. Model-routing decisions measurably reduce unnecessary generative-model calls in integration tests.
11. JEV unavailability does not break core browsing, ingestion, or manual workflow.
12. Provider/model/version used for every decision is visible in `/ops` or the opportunity activity view.

---

## 43. Prompt runtime, versioning, evaluation, and deployment

### 43.1 Prompt loader

The application must never import a raw prompt constant from a business-service module.

Correct:

```python
prompt = prompt_registry.load("requirement_extraction_a", version="active")
```

Incorrect:

```python
PROMPT = "You are a government contract..."
```

inside `compliance/extractor.py`.

### 43.2 Exact reproducibility

Every AI run must be reproducible from recorded metadata:

```text
provider
model
prompt name
prompt version
prompt hash
schema version
generation settings
input snapshot hash
context manifest
source snapshot IDs
```

### 43.3 Context manifest

Do not record only a giant concatenated input string.

Record a context manifest like:

```json
{
  "opportunity_id": 123,
  "source_snapshots": [55, 56],
  "files": [
    {"file_id": 11, "sha256": "...", "pages": "1-22"},
    {"file_id": 12, "sha256": "...", "pages": "1-4"}
  ],
  "structured_inputs": {
    "pricing_version": "abc123",
    "company_facts_version": "v4"
  }
}
```

### 43.4 Context construction strategy

Do not rely on enormous context windows just because a provider supports them.

Preferred hierarchy:

```text
document inventory
    ↓
extract/search relevant source ranges
    ↓
structured facts
    ↓
task-specific context
    ↓
model call
```

For whole-package extraction where broad context is required, use chunked/hierarchical processing with source IDs preserved.

### 43.5 Model settings by task

Initial defaults; keep configurable and verify provider support:

| Task | Temperature / randomness | Reasoning | Output |
|---|---:|---|---|
| Requirement extraction | lowest practical | normal | strict JSON |
| Reconciliation | lowest practical | normal/high | strict JSON |
| Compliance validation | lowest practical | high when needed | strict JSON |
| Reviewer comment validation | low | normal | strict JSON |
| Market analysis | low | normal | strict JSON |
| Pricing analysis | low | normal | strict JSON |
| Proposal drafting | low/moderate | normal | structured JSON sections |
| Proposal red-team | low | high when available | strict JSON |
| Amendment analysis | lowest practical | high | strict JSON |
| Outcome classification | lowest practical | normal | strict JSON |

Do not assume every provider exposes the same controls.

### 43.6 Prompt regression dataset

Directory:

```text
tests/fixtures/prompts/
├── solicitation_analysis/
├── requirement_extraction/
├── amendment_analysis/
├── comment_validation/
├── proposal_drafting/
├── proposal_red_team/
├── compliance_validation/
├── proposal_coverage/
└── submission_preflight/
```

Each case includes:

```text
inputs
expected required facts/classes
forbidden hallucinations
expected source refs
minimum acceptable recall
maximum false-positive/false-satisfied threshold
notes
```

### 43.7 Evaluation metrics by prompt class

Requirement extraction:

```text
mandatory requirement recall
critical requirement recall
source citation accuracy
false requirement rate
```

Compliance validation:

```text
false-satisfied rate
unknown handling accuracy
evidence-link accuracy
critical blocker recall
```

Amendment analysis:

```text
material change recall
false change rate
affected-requirement recall
```

Comment validation:

```text
evidence-grounding rate
correct insufficient-evidence behavior
unsupported-agreement rate
```

Proposal drafting:

```text
requirement coverage
unsupported-claim count
blocker correctness
fact-reference accuracy
```

Proposal red-team:

```text
critical issue recall
false finding rate
evidence linkage
```

Submission pre-flight:

```text
critical blocker recall
false-ready rate
unresolved-state detection
```

### 43.8 Prompt activation flow

```text
new prompt version
    ↓
syntax/template validation
    ↓
schema validation
    ↓
security/injection fixtures
    ↓
task regression suite
    ↓
compare against active version
    ↓
PASS?
  /    \
NO      YES
↓        ↓
reject   activate
```

Safety-critical prompt versions must not be automatically activated merely because their average score improves.

Any regression in a critical safety metric can block activation.

### 43.9 Prompt rollback

Activation must be reversible:

```text
govcon prompts activate requirement_extraction_a@v4
govcon prompts rollback requirement_extraction_a
```

Rollback changes only the active version. Historical runs keep their original prompt metadata.

### 43.10 Prompt observability UI

`/ops` or a dedicated admin view should show:

```text
active prompt versions
provider/model
last evaluation date
regression suite status
prompt hash
number of calls
token/cost totals
JSON/schema error rate
fallback rate
average latency
```

Opportunity activity should show which prompt/model produced each AI analysis.

### 43.11 Prompt acceptance criteria

Before v1 is considered AI-build complete:

1. no production AI task prompt is hardcoded inside a business-service module
2. all production prompts are source-controlled and versioned
3. exact prompt hashes are persisted for every AI run
4. output schemas are versioned
5. malformed structured output fails closed
6. source prompt-injection fixtures are included
7. requirement extraction has two independent prompt strategies
8. compliance prompts pass compliance regression gates
9. proposal drafting has explicit no-fabrication/blocker behavior
10. reviewer-comment validation preserves the human comment unchanged
11. amendment prompt identifies affected requirements
12. proposal coverage prompt maps requirements to final proposal evidence
13. submission-preflight prompt cannot override deterministic blockers
14. all 13 JEV bundles have versioned decision-spec files
15. prompt activation is gated and rollback is supported
16. prompt/model metadata is visible in operations/audit views

---

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

**Do not begin Phase 20 in the same implementation run.**
