# GovCon v2.5 — Phase 15 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `21. Phase 15 — Outcome learning & analytics`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

Win/loss/no-bid outcomes, learning analytics, evidence-constrained outcome classification.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 8, 10, 11, 14**.

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

- No invented causal explanation for losses.
- No win-profile learning before minimum evidence threshold.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 21. Phase 15 — Outcome learning & analytics

**Goal:** improve future bid decisions using the user's own history.

### Capture on every terminal outcome

For `no_bid`:

```text
reason
missing capability
margin
deadline
supplier availability
eligibility
compliance issue
competition
strategic choice
other
```

For `lost`:

```text
winner
award amount
known winning price
government feedback
debrief
suspected/known reason
pricing difference
technical/compliance issue
other
```

For `won`:

```text
award value
margin
supplier
agency
PSC
NAICS
delivery terms
proposal version
key strengths
```

### Analytics

Provide descriptive history, not unsupported causal claims.

Examples:

```text
Win rate by PSC
Win rate by agency
Win rate by bid size
Average margin on wins
No-bid reasons
Loss reasons
Most common competitors
Most reliable suppliers
Average days from discovery to submission
Average amendment count
Compliance issues discovered late
```

### Recommendation learning

Future bid recommendations may use historical outcomes only after enough data exists.

Rules:

- <3 wins: do not create "win profile."
- small samples must be visibly labeled.
- do not overstate correlation as causation.

### Acceptance criteria

- Outcome can be recorded in UI/MCP.
- Analytics update without manual SQL.
- Future decision report can reference prior similar wins/losses.

---


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

### 39.8 `outcome_analysis_v1.md`

```text
ROLE
You are an evidence-constrained outcome classifier.

OBJECTIVE
Structure documented win/loss/no-bid evidence for future analytics without
inventing causal explanations.

INPUTS
- outcome
- award result when known
- debrief/government feedback
- pricing evidence
- reviewer notes
- compliance findings
- sourcing history

TASK
Classify supported factors involving:
- pricing
- compliance
- sourcing
- deadline
- eligibility
- delivery
- competition
- administrative issue
- strategic no-bid reason

RULES
- Government feedback is stronger evidence than speculation.
- If the cause is not established, return UNKNOWN.
- Do not claim the business lost because of price merely because another
  award value differs.
- Preserve direct feedback separately from inferred signals.

OUTPUT
Return only JSON conforming to outcome_analysis.v1.
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

**Do not begin Phase 16 in the same implementation run.**
