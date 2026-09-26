# GovCon v2.5 — Phase 8 Build Spec

**Master specification:** `MASTER_SPEC_v2.5.md`  
**Canonical master section:** `14. Phase 8 — JEV preliminary decision engine + AI decision package`  
**Implementation rule:** The master spec is authoritative; this file is the scoped execution document for this phase.

## Phase objective

JEV decision provider, all decision bundles, preliminary bid recommendation, AI decision package.

## Dependencies

The following phases must be implemented and passing before starting this phase: **0, 2, 5, 6, 7**.

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

- No human approval bypass.
- No proposal generation.
- No final submission decision.

---

## Canonical requirements from MASTER_SPEC_v2.5

## 14. Phase 8 — JEV preliminary decision engine + AI decision package

**Goal:** let AI and JEV finish the analytical work before human review. Produce a complete, evidence-backed decision package that reviewers can evaluate quickly.

### Decision pipeline

```text
Hard eligibility rules
        ↓
Capability / deadline / pricing / history evidence
        ↓
Optional Jev structured decision
        ↓
LLM analysis
        ↓
Combined recommendation
        ↓
HUMAN DECISION
```

### Hard-rule examples

Examples only; keep configurable:

- solicitation already closed
- mandatory set-aside not satisfied
- impossible delivery requirement
- mandatory certification absent
- required product cannot be sourced
- prohibited country-of-origin conflict identified
- missing mandatory registration/status

Do not automatically mark `no_bid` solely from an LLM.

### Decision factors

Store separate factor scores/evidence for:

```text
capability fit
product/source availability
historical pricing
expected margin
past-performance fit
delivery feasibility
competition
set-aside eligibility
compliance risk
deadline risk
submission complexity
```

### Recommendation result

```json
{
  "recommendation": "bid|no_bid|review|insufficient_information",
  "score": 0.0,
  "strengths": [],
  "risks": [],
  "missing_information": [],
  "evidence": [],
  "factor_scores": {}
}
```

### Output of Phase 8: AI Decision Package

The phase must produce one review-ready package containing:

```text
Opportunity summary
Eligibility status
Capability fit
Product/source status
Supplier findings
Historical award/pricing intelligence
Expected margin / pricing position
Compliance matrix summary
Deadline risk
Execution risk
Competition/incumbent signals
JEV preliminary recommendation
Why BID
Why NO BID
Missing information
Open questions
Source evidence
```

At this stage the system does **not** require a human decision unless a hard blocker or low-confidence exception has been triggered.

The normal next state is:

```text
READY_FOR_COLLABORATIVE_REVIEW
```

### Acceptance criteria

- The same opportunity can have multiple AI decision runs over time.
- Human decision remains authoritative.
- Decision explains what evidence led to the recommendation.
- Missing data produces `review` or `insufficient_information`, not fabricated certainty.

---


---

## Phase-specific cross-cutting requirements

The following master-spec material applies directly to this phase and is reproduced here so the coding agent does not miss it.

## 30. JEV decision catalog

**Goal:** define the complete set of structured decisions JEV may handle across the GovCon lifecycle.

### Core implementation rule

Do **not** implement every question below as a separate network call.

The catalog defines the **logical questions** the system may ask. Production code should group related questions into approximately **10-13 decision bundles**, reuse the same structured state, and avoid unnecessary calls.

JEV is appropriate for:

```text
Choice        → one option among known outcomes
Score         → ordered risk/strength/priority
Noul/Boolean  → yes/no gating
Classification → route/categorize
```

JEV is **not** the authoritative source for:

```text
deadlines
quantities
prices
award data
certifications
solicitation requirements
supplier inventory
portal submission status
government feedback
```

Those facts must come from source data, deterministic code, user-entered data, or extraction with traceable evidence.

---

### 30.1 Opportunity ingestion and first-pass triage

1. **Is this opportunity potentially relevant to our business?**  
   Output: `relevant | not_relevant | review`

2. **How strong is the initial opportunity fit?**  
   Output: `very_low | low | medium | high | very_high`

3. **Is this opportunity worth deeper AI analysis?**  
   Output: `yes | no`

4. **Should the platform spend tokens downloading/analyzing all attachments?**  
   Output: `yes | no | review`

5. **Is this opportunity urgent enough to prioritize now?**  
   Output: `low | medium | high | critical`

6. **Should this opportunity be surfaced in the current alert digest?**  
   Output: `yes | no`

7. **Is the semantic match strong enough to recommend despite weak keyword matching?**  
   Output: `yes | no | review`

8. **Does this opportunity look like a semantic near-duplicate worth human attention?**  
   Output: `duplicate | distinct | review`

**Rule:** exact duplicates remain a deterministic hash/ID problem. JEV is only for ambiguous semantic duplication.

---

### 30.2 Opportunity prioritization

9. **How attractive is this opportunity overall?**  
   Output: `very_low | low | medium | high | very_high`

10. **What priority should this opportunity receive?**  
    Output: `low | medium | high | critical`

11. **Should this opportunity be reviewed before other open opportunities?**  
    Output: `yes | no`

12. **Is the remaining deadline sufficient to realistically prepare a bid?**  
    Output: `yes | no | risky`

13. **How severe is deadline risk?**  
    Output: `low | medium | high | critical`

14. **Is there enough information to make a meaningful next-step decision?**  
    Output: `yes | no`

15. **Should the opportunity be escalated to human review immediately?**  
    Output: `yes | no`

---

### 30.3 Eligibility and qualification

16. **Does the opportunity appear to fit our business capabilities?**  
    Output: `yes | no | partial`

17. **How strong is the capability match?**  
    Output: `very_low | low | medium | high | very_high`

18. **Is the set-aside / eligibility situation clear enough to proceed?**  
    Output: `yes | no | review`

19. **Are there apparent eligibility risks requiring human verification?**  
    Output: `yes | no`

20. **Are mandatory certifications likely to become a blocker?**  
    Output: `low | medium | high | critical`

21. **Does the delivery location create meaningful execution risk?**  
    Output: `low | medium | high`

22. **Is the requested delivery schedule realistically achievable?**  
    Output: `yes | no | review`

23. **Does country-of-origin compliance appear risky?**  
    Output: `low | medium | high | critical`

24. **Is there a qualification issue serious enough to recommend stopping evaluation?**  
    Output: `yes | no`

**Rule:** hard facts such as deadline expiration, set-aside type, or missing registration are determined by code/source data first. JEV evaluates the impact of those facts.

---

### 30.4 Product and supplier sourcing

25. **Does this supplier/product appear suitable for the solicitation?**  
    Output: `yes | no | review`

26. **How strong is this product match?**  
    Output: `very_low | low | medium | high | very_high`

27. **Is the proposed substitute/equivalent product acceptable enough for further review?**  
    Output: `yes | no | review`

28. **Does the supplier quote look commercially viable?**  
    Output: `yes | no | marginal`

29. **How risky is this supplier?**  
    Output: `low | medium | high`

30. **Is supplier availability sufficient to support a bid?**  
    Output: `yes | no | review`

31. **Does supplier lead time fit the government delivery requirement?**  
    Output: `yes | no | risky`

32. **Should additional supplier quotes be obtained?**  
    Output: `yes | no`

33. **Which supplier quote should receive priority for human review?**  
    Output: `supplier_choice`

34. **Is the sourcing evidence strong enough to move from sourcing to pricing?**  
    Output: `yes | no`

---

### 30.5 Historical award and market intelligence

35. **How relevant are these historical awards to the current opportunity?**  
    Output: `low | medium | high`

36. **Is historical pricing sufficiently comparable to help price this bid?**  
    Output: `yes | no | partial`

37. **Does historical pricing indicate our supplier cost is competitive?**  
    Output: `yes | no | unclear`

38. **How intense does historical competition appear?**  
    Output: `low | medium | high`

39. **Is there evidence of a strong incumbent advantage?**  
    Output: `low | medium | high`

40. **Are there enough comparable awards to rely meaningfully on market history?**  
    Output: `yes | no`

41. **Should deeper competitor research be performed?**  
    Output: `yes | no`

42. **Does this appear to be a recompete opportunity worth prioritizing?**  
    Output: `yes | no | review`

---

### 30.6 Pricing decisions

43. **Does the current quote price appear commercially reasonable relative to known evidence?**  
    Output: `yes | no | review`

44. **How attractive is the expected margin?**  
    Output: `poor | marginal | good | strong`

45. **Is the proposed margin too low for the execution risk?**  
    Output: `yes | no`

46. **Is the proposed price worth submitting given historical award pricing?**  
    Output: `yes | no | review`

47. **Is more pricing research needed before approval?**  
    Output: `yes | no`

48. **Is supplier cost volatility a meaningful bid risk?**  
    Output: `low | medium | high`

49. **Should the opportunity be rejected because economics are unattractive?**  
    Output: `yes | no | review`

50. **Does the price require human escalation before proceeding?**  
    Output: `yes | no`

**Rule:** JEV never computes or commits final price. Python computes cost, markup, margin, historical range, and other numerical facts first.

---

### 30.7 Core Bid / No-Bid decision

51. **Should we bid on this opportunity?**  
    Output: `bid | no_bid | review`

52. **How strong is this opportunity?**  
    Output: `very_low | low | medium | high | very_high`

53. **How confident is the available evidence supporting the bid recommendation?**  
    Output: `low | medium | high`

54. **Is there enough information to recommend BID?**  
    Output: `yes | no`

55. **Does any unresolved issue justify NO BID?**  
    Output: `yes | no`

56. **Should a human decide this opportunity instead of continuing automatically?**  
    Output: `yes | no`

57. **What is the overall execution risk?**  
    Output: `low | medium | high | critical`

58. **What is the overall commercial attractiveness?**  
    Output: `very_low | low | medium | high | very_high`

59. **What is the overall compliance risk?**  
    Output: `low | medium | high | critical`

60. **Should this opportunity move toward `bid_approved` pending human confirmation?**  
    Output: `yes | no`

**Rule:** only the human decision field can authorize the transition to `bid_approved`.

---

### 30.8 Compliance matrix decisions

61. **Is this extracted requirement likely mandatory?**  
    Output: `yes | no | review`

62. **Does the current response appear to satisfy this requirement?**  
    Output: `satisfied | missing | review`

63. **How serious is this missing requirement?**  
    Output: `low | medium | high | critical`

64. **Should this requirement block submission readiness?**  
    Output: `yes | no`

65. **Does this requirement need human interpretation?**  
    Output: `yes | no`

66. **Is the supporting evidence sufficient?**  
    Output: `yes | no | partial`

67. **Does this requirement appear inconsistent with another requirement?**  
    Output: `yes | no | review`

68. **Does this amendment materially change our compliance position?**  
    Output: `yes | no`

69. **Does this amendment require proposal revision?**  
    Output: `yes | no`

70. **Does this amendment require the bid/no-bid decision to be revisited?**  
    Output: `yes | no`

---

### 30.9 Proposal drafting workflow — post-approval only

JEV does not write proposal prose. Proposal generation occurs only after collaborative review and human approval-to-bid. JEV controls routing, severity, and review escalation.

71. **Is this proposal section ready for human review?**  
    Output: `yes | no`

72. **Does this section sufficiently answer its assigned requirements?**  
    Output: `yes | no | partial`

73. **Does this section contain unsupported claims?**  
    Output: `yes | no | review`

74. **Is this section too weak to move forward?**  
    Output: `yes | no`

75. **Should this section be regenerated?**  
    Output: `yes | no`

76. **Does this section require a more capable LLM for review?**  
    Output: `yes | no`

77. **Does this section require human subject-matter review?**  
    Output: `yes | no`

78. **What is the section quality level?**  
    Output: `poor | fair | good | strong`

79. **What is the compliance risk of this section?**  
    Output: `low | medium | high`

80. **Is another red-team pass warranted?**  
    Output: `yes | no`

---

### 30.10 Red-team / proposal review routing

81. **Is this review finding materially important?**  
    Output: `yes | no | review`

82. **How severe is this finding?**  
    Output: `minor | major | critical`

83. **Must this issue be fixed before submission?**  
    Output: `yes | no`

84. **Should this issue be escalated to the user?**  
    Output: `yes | no`

85. **Does this issue affect bid viability?**  
    Output: `yes | no`

86. **Does this finding require pricing to be revisited?**  
    Output: `yes | no`

87. **Does this finding require supplier verification?**  
    Output: `yes | no`

88. **Does this finding require proposal rewrite?**  
    Output: `yes | no`

---

### 30.11 Submission-readiness decisions

89. **Is this bid ready for submission?**  
    Output: `ready | not_ready | review`

90. **Is there any unresolved issue serious enough to block submission?**  
    Output: `yes | no`

91. **Is the required document package complete?**  
    Output: `yes | no | review`

92. **Are remaining compliance issues acceptable for human override consideration?**  
    Output: `yes | no | review`

93. **Is submission deadline risk now critical?**  
    Output: `yes | no`

94. **Does the final proposal need another review cycle?**  
    Output: `yes | no`

95. **Should the user be immediately alerted?**  
    Output: `yes | no`

96. **Is manual verification required before submission?**  
    Output: `yes | no`

**Rule:** JEV may return `ready`; only application rules + human action can move the pursuit to submitted.

---

### 30.12 Amendment impact monitoring

97. **Is this amendment material?**  
    Output: `yes | no`

98. **How severe is the amendment's impact?**  
    Output: `low | medium | high | critical`

99. **Does this amendment change pricing?**  
    Output: `yes | no | review`

100. **Does this amendment change sourcing requirements?**  
     Output: `yes | no`

101. **Does this amendment change delivery requirements?**  
     Output: `yes | no`

102. **Does this amendment invalidate part of our proposal?**  
     Output: `yes | no | review`

103. **Must the compliance matrix be regenerated or re-reviewed?**  
     Output: `yes | no`

104. **Must the bid decision be reassessed?**  
     Output: `yes | no`

105. **Should the user receive an immediate amendment alert?**  
     Output: `yes | no`

---

### 30.13 Post-submission workflow

106. **Does this government communication require action?**  
     Output: `yes | no`

107. **How urgent is the requested action?**  
     Output: `low | medium | high | critical`

108. **What type of communication is this?**  
     Output: `amendment | clarification | award_notice | rejection | request_for_information | general | review`

109. **Does this communication require a response from the user?**  
     Output: `yes | no`

110. **Does the response require proposal or pricing changes?**  
     Output: `yes | no`

111. **Should this opportunity return to an earlier review workflow stage?**  
     Output: `yes | no`

---

### 30.14 Win / Loss / No-Bid learning

JEV classifies documented evidence. It must not invent causal explanations.

112. **What category best describes the documented no-bid reason?**  
     Output: configured `no_bid_reason` classification

113. **What category best describes the documented loss reason?**  
     Output: configured `loss_reason` classification

114. **Was pricing likely a meaningful factor based on available evidence?**  
     Output: `yes | no | unknown`

115. **Was compliance a meaningful factor based on available evidence?**  
     Output: `yes | no | unknown`

116. **Was sourcing a meaningful factor based on available evidence?**  
     Output: `yes | no | unknown`

117. **Was deadline pressure a meaningful factor based on available evidence?**  
     Output: `yes | no | unknown`

118. **Should this lesson affect future bidding analysis?**  
     Output: `yes | no`

119. **Is this outcome sufficiently similar to future opportunities to be useful evidence?**  
     Output: `yes | no`

120. **How relevant is this historical outcome to the current opportunity?**  
     Output: `low | medium | high`

**Rule:** if no official feedback or reliable evidence exists, causal factors remain `unknown`.

---

### 30.15 AI model routing and cost control

121. **Does this task require a generative LLM at all?**  
     Output: `yes | no`

122. **Is a low-cost model sufficient?**  
     Output: `yes | no`

123. **Does this task require a high-capability model?**  
     Output: `yes | no`

124. **Does this task require second-model review?**  
     Output: `yes | no`

125. **Is external AI allowed for this data classification under current policy?**  
     Output: `allow | block | human_review`

126. **Is the extracted information uncertain enough to justify another model call?**  
     Output: `yes | no`

127. **Should additional tokens/budget be spent analyzing this opportunity?**  
     Output: `yes | no`

**Rule:** hard data-classification blocks remain deterministic policy rules. JEV may help route ambiguous cases but cannot override a hard block.

---

### 30.16 Workflow routing

128. **What should happen next?**  
     Output: `dismiss | analyze | review | pursue | wait`

129. **Which workflow stage should receive this item?**  
     Output: allowed workflow stage

130. **Does this require user attention now?**  
     Output: `yes | no`

131. **Is automated processing safe to continue?**  
     Output: `yes | no`

132. **Is sufficient evidence available to proceed automatically?**  
     Output: `yes | no`

133. **Should this task be deferred until more information arrives?**  
     Output: `yes | no`

134. **Should analysis be re-run because material source data changed?**  
     Output: `yes | no`

---


## 31. JEV decision bundles to implement

The 134 logical questions above should be implemented through a smaller set of reusable bundles.

### Bundle 1 — `opportunity_triage`

Used immediately after watchlist/semantic matching.

Suggested outputs:

```json
{
  "relevance": "relevant",
  "initial_fit": "high",
  "priority": "high",
  "deep_analysis_required": true,
  "attachment_analysis_required": true,
  "deadline_risk": "medium",
  "human_review_required": false,
  "next_action": "analyze"
}
```

Covers primarily questions:

```text
1-15
121-123
127-134 where applicable
```

---

### Bundle 2 — `eligibility_and_execution`

Used after structured solicitation extraction.

Suggested outputs:

```json
{
  "capability_match": "high",
  "eligibility_clear": true,
  "certification_blocker_risk": "low",
  "delivery_feasibility": "yes",
  "country_of_origin_risk": "medium",
  "execution_risk": "medium",
  "stop_evaluation": false,
  "human_review_required": true
}
```

Covers:

```text
16-24
```

---

### Bundle 3 — `sourcing_and_supplier`

Run after supplier/product facts are available.

Suggested outputs:

```json
{
  "product_match": "high",
  "supplier_risk": "low",
  "supplier_availability": "yes",
  "lead_time_fit": "yes",
  "additional_quotes_required": true,
  "preferred_quote_id": 123,
  "ready_for_pricing": true
}
```

Covers:

```text
25-34
```

---

### Bundle 4 — `market_and_pricing`

Run after historical awards and supplier pricing exist.

Suggested outputs:

```json
{
  "historical_comparability": "high",
  "supplier_cost_competitiveness": "yes",
  "competition_level": "medium",
  "incumbent_advantage": "low",
  "margin_quality": "good",
  "pricing_research_required": false,
  "commercial_attractiveness": "high",
  "pricing_human_review_required": false
}
```

Covers:

```text
35-50
```

---

### Bundle 5 — `bid_decision`

The core pursuit recommendation.

Suggested outputs:

```json
{
  "recommendation": "bid",
  "opportunity_strength": "high",
  "evidence_confidence": "high",
  "sufficient_information": true,
  "unresolved_no_bid_issue": false,
  "execution_risk": "medium",
  "commercial_attractiveness": "high",
  "compliance_risk": "low",
  "human_review_required": true,
  "recommend_bid_approval": true
}
```

Covers:

```text
51-60
```

The system stores JEV's result, but only the user can create the authoritative human decision.

---

### Bundle 6 — `compliance_and_amendment`

Used after the evidence-backed compliance subsystem has produced structured findings and whenever an amendment arrives.

JEV receives structured compliance state; it does not replace requirement extraction, deterministic validators, clause checks, or source evidence.

Suggested outputs:

```json
{
  "requirement_decisions": [
    {
      "requirement_id": 1,
      "mandatory": true,
      "status_assessment": "missing",
      "severity": "critical",
      "blocks_submission": true,
      "human_interpretation_required": false
    }
  ],
  "mandatory_total": 47,
  "mandatory_unresolved": 4,
  "critical_total": 12,
  "critical_unresolved": 1,
  "false_satisfied_risk": "low",
  "second_validation_required": true,
  "amendment_material": true,
  "proposal_revision_required": true,
  "bid_reassessment_required": false,
  "immediate_alert_required": true
}
```

Covers:

```text
61-70
97-105
```

---

### Bundle 7 — `collaborative_review_synthesis`

Run only after all required human reviewers mark their review complete.

Suggested outputs:

```json
{
  "review_policy": "conditional",
  "required_review_count": 1,
  "completed_review_count": 1,
  "quorum_satisfied": true,
  "second_review_required": false,
  "second_review_reason": null,
  "reviewer_alignment": "single_reviewer",
  "shared_concerns": ["delivery"],
  "material_disagreements": [],
  "new_material_risks": [],
  "unresolved_questions": ["confirm delivered-by date"],
  "evidence_confidence": "medium",
  "recommendation": "review",
  "approval_gate_status": "human_decision_required"
}
```

Inputs include:

```text
AI decision package
reviewer recommendations
reviewer comments
AI comment validations
open issues
latest compliance/pricing/sourcing state
```

This bundle does not approve the bid. It prepares the final approval gate.

---

### Bundle 8 — `proposal_review`

Run on proposal sections or the full selected version.

Suggested outputs:

```json
{
  "ready_for_human_review": true,
  "requirement_coverage": "partial",
  "unsupported_claims_present": false,
  "quality": "good",
  "compliance_risk": "medium",
  "regeneration_required": false,
  "stronger_model_required": false,
  "human_sme_review_required": true,
  "another_red_team_pass": true
}
```

Covers:

```text
71-88
```

---

### Bundle 9 — `submission_readiness`

Run immediately before presenting the final submission-ready state.

Suggested outputs:

```json
{
  "status": "not_ready",
  "blocking_issue_exists": true,
  "document_package_complete": false,
  "override_candidate": false,
  "deadline_critical": false,
  "another_review_required": false,
  "human_verification_required": true,
  "immediate_alert_required": false
}
```

Covers:

```text
89-96
```

---

### Bundle 10 — `post_submission_routing`

Used for government communications after submission.

Suggested outputs:

```json
{
  "action_required": true,
  "urgency": "high",
  "communication_type": "clarification",
  "response_required": true,
  "proposal_or_pricing_change_required": false,
  "return_to_review_workflow": true
}
```

Covers:

```text
106-111
```

---

### Bundle 11 — `outcome_learning`

Run only on available outcome evidence.

Suggested outputs:

```json
{
  "no_bid_reason": null,
  "loss_reason": "price",
  "pricing_factor": "yes",
  "compliance_factor": "unknown",
  "sourcing_factor": "no",
  "deadline_factor": "no",
  "use_for_future_analysis": true,
  "similarity_relevance": "high"
}
```

Covers:

```text
112-120
```

Never replace `unknown` with a guessed causal story.

---

### Bundle 12 — `model_router`

Used before expensive generative-model operations.

Suggested outputs:

```json
{
  "generative_llm_required": true,
  "low_cost_model_sufficient": false,
  "high_capability_model_required": true,
  "second_model_review_required": false,
  "external_ai_policy": "allow",
  "another_model_call_warranted": true,
  "additional_budget_warranted": true
}
```

Covers:

```text
121-127
```

Hard security/data-classification policies execute before or after this bundle as appropriate and always override JEV.

---

### Bundle 13 — `workflow_router`

Generic fallback routing decision for state-machine transitions.

Suggested outputs:

```json
{
  "next_action": "review",
  "recommended_stage": "review",
  "user_attention_required": true,
  "safe_to_continue_automatically": false,
  "sufficient_evidence": false,
  "defer_until_more_information": true,
  "rerun_analysis": false
}
```

Covers:

```text
128-134
```

Prefer a specialized bundle over this generic bundle whenever one exists.

---


## 32. JEV state contract

Each bundle must consume structured, source-backed state.

Example:

```json
{
  "opportunity": {
    "id": 123,
    "source": "sam",
    "psc": "6515",
    "naics": "423450",
    "set_aside": "SBA",
    "days_remaining": 12,
    "estimated_value_min": null,
    "estimated_value_max": null
  },
  "eligibility": {
    "sam_active": true,
    "set_aside_match": true,
    "mandatory_certifications_met": true
  },
  "sourcing": {
    "product_found": true,
    "supplier_count": 3,
    "best_supplier_cost": 8500,
    "lead_time_days": 10
  },
  "pricing": {
    "proposed_price": 10200,
    "margin_pct": 20.0,
    "historical_comparable_count": 8,
    "historical_median": 10850
  },
  "compliance": {
    "mandatory_total": 22,
    "mandatory_satisfied": 20,
    "mandatory_missing": 1,
    "needs_review": 1
  },
  "past_performance": {
    "match": "medium"
  },
  "evidence_refs": []
}
```

### State rules

- use normalized values
- attach source/evidence references
- distinguish `false`, `null`, and `unknown`
- never convert unknown values to zero
- include timestamp/snapshot version where material
- keep stable schema versions
- reject invalid states before JEV call

---


## 33. JEV result persistence

Add a dedicated table so decision history is auditable.

```sql
CREATE TABLE decision_runs (
  id                  BIGSERIAL PRIMARY KEY,
  opportunity_id      BIGINT REFERENCES opportunities(id),

  bundle_name         TEXT NOT NULL,
  bundle_version      TEXT NOT NULL,

  provider            TEXT NOT NULL DEFAULT 'jev',
  model               TEXT,

  decision_spec_name  TEXT,
  decision_spec_hash  TEXT,
  schema_version      TEXT,

  input_state         JSONB NOT NULL,
  input_state_hash    TEXT NOT NULL,

  result              JSONB NOT NULL,

  confidence          NUMERIC,
  cost                NUMERIC,
  latency_ms           INTEGER,

  source_snapshot_ids BIGINT[],
  supersedes_run_id   BIGINT REFERENCES decision_runs(id),

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON decision_runs
  (opportunity_id, bundle_name, created_at);
```

Rules:

- Never overwrite decision runs.
- Re-run when material state changes.
- Link to opportunity snapshots where possible.
- Store bundle/model/version for reproducibility.
- Do not store secrets in state/result JSON.

---


## 34. JEV confidence, fallback, and escalation rules

JEV output is a decision signal, not truth.

Application logic must support:

```text
high-confidence + low-consequence
    → continue automatically where allowed

low-confidence
    → human review or fallback provider

conflicting hard rule
    → hard rule wins

missing evidence
    → review / insufficient information

consequential action
    → human approval regardless of confidence
```

### Consequential actions requiring human authority

JEV must never autonomously:

```text
approve a bid
commit final pricing
represent a certification as true
send a final proposal
submit through a portal
withdraw a submitted bid
accept an award
make a legal/compliance certification
```

### Fallback order

Recommended:

```text
1. deterministic hard rules
2. JEV
3. RuleDecisionProvider fallback where possible
4. LLMDecisionProvider only when appropriate
5. human review
```

Do not silently replace a failed JEV call with a generative LLM and present the result as equivalent. Record the provider used.

---


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

## 42. JEV decision-spec prompt contracts

JEV decision files are not long prose prompts. They are versioned decision specifications.

### 42.1 Common JEV YAML contract

```yaml
name: bid_decision
version: v1
provider: jev
input_schema: bid_decision_state.v1

objective: >
  Produce structured bid/no-bid routing signals from supplied,
  source-backed state. Do not invent missing facts.

questions:
  - id: recommendation
    type: choice
    question: Should this opportunity be recommended for bidding?
    choices: [BID, NO_BID, REVIEW]

  - id: opportunity_strength
    type: score
    question: How strong is the opportunity based on supplied evidence?
    scale: [VERY_LOW, LOW, MEDIUM, HIGH, VERY_HIGH]

  - id: human_review_required
    type: boolean
    question: Is human review required before this opportunity can progress?

policy:
  unknown_is_not_negative: true
  hard_rules_override: true
  consequential_action_requires_human: true
```

### 42.2 Required JEV spec files

Claude Code must materialize all of these using the logical questions and outputs already defined in §§30–31:

```text
opportunity_triage_v1.yaml
eligibility_execution_v1.yaml
sourcing_supplier_v1.yaml
market_pricing_v1.yaml
bid_decision_v1.yaml
compliance_amendment_v1.yaml
collaborative_review_v1.yaml
proposal_review_v1.yaml
submission_readiness_v1.yaml
post_submission_routing_v1.yaml
outcome_learning_v1.yaml
model_router_v1.yaml
workflow_router_v1.yaml
```

### 42.3 JEV decision-spec rules

Every JEV spec must define:

```text
name
version
input schema
objective
question ID
question type
question wording
choice/score scale when applicable
unknown handling
confidence/escalation policy
human-approval policy
```

Question wording changes require a version increment.

The runtime stores:

```text
decision spec name
version
content hash
JEV model
input-state hash
result
confidence
cost
latency
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

**Do not begin Phase 9 in the same implementation run.**
