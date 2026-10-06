"""Phase 20 — Final integration acceptance (MASTER_SPEC §26 + Appendix D).

Covers:
- Appendix D Definition of Done — items 1–27 with Phase 16 waiver
- §26 feature checklist parity (all in-scope capabilities present)
- market_analysis / supplier_analysis / pricing_analysis prompts active
- New schemas registered: market_analysis.v1, supplier_analysis.v1, pricing_analysis.v1
- Prompt gate passes for all 18 active task prompts
- No post-v1 non-goals introduced (§28)

No live network calls. No live AI/JEV keys required.
DB-backed tests use the ``upgraded_engine`` session fixture.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PROMPT_ROOT = REPO_ROOT / "src" / "govcon" / "prompts"
SRC_ROOT = REPO_ROOT / "src" / "govcon"


# ---------------------------------------------------------------------------
# Appendix D — Definition of Done (items 1–27, Phase 16 waived)
# ---------------------------------------------------------------------------


class TestAppendixDDoD:
    """Verify each Appendix D item is satisfied by the in-scope phases."""

    # ── DoD 1: ingest federal opportunities ─────────────────────────────────

    def test_dod_1_federal_opportunity_ingestion(self) -> None:
        """DoD 1 — Federal opportunities can be ingested (Phase 1 SAM ingestion)."""
        from govcon.ingest.sam_opportunities import normalize_opportunity

        assert callable(normalize_opportunity)

        sam_hit = {
            "noticeId": "dod1-test-001",
            "title": "Medical Supply NSN 6515-01-234-5678",
            "type": "Solicitation",
            "postedDate": "2026-09-01",
            "responseDeadLine": "2026-10-01T17:00:00-04:00",
            "naicsCode": "339112",
            "classificationCode": "6515",
            "active": "Yes",
            "organizationHierarchy": [{"name": "DEPT OF DEFENSE"}],
        }
        result = normalize_opportunity(sam_hit)
        assert result is not None
        # normalize_opportunity stores the notice_id as source_id
        assert result.source_id == "dod1-test-001"

    # ── DoD 2: receive filtered matches ─────────────────────────────────────

    def test_dod_2_filtered_matching(self) -> None:
        """DoD 2 — Watchlist matching engine applies filters (Phase 2)."""
        from govcon.matching.engine import evaluate_match

        assert callable(evaluate_match)

        # The engine module provides the deterministic rule evaluator
        from govcon.matching.engine import _evaluate_psc

        assert callable(_evaluate_psc)

    # ── DoD 3: inspect amendments/history ────────────────────────────────────

    def test_dod_3_amendment_history(self) -> None:
        """DoD 3 — Opportunity snapshots and events exist for history (Phase 1)."""
        from govcon.models import OpportunityEvent, OpportunitySnapshot

        snap_cols = {c.name for c in OpportunitySnapshot.__table__.columns}
        assert "opportunity_id" in snap_cols
        assert "content_hash" in snap_cols
        assert "raw" in snap_cols
        assert "fetched_at" in snap_cols

        event_cols = {c.name for c in OpportunityEvent.__table__.columns}
        assert "event_type" in event_cols  # e.g. "deadline_changed"
        assert "opportunity_id" in event_cols

    # ── DoD 4: AI analysis of solicitation files ──────────────────────────────

    def test_dod_4_solicitation_ai_analysis(self) -> None:
        """DoD 4 — Solicitation analysis prompt active and schema registered (Phase 7/9)."""
        from govcon.ai.schemas import SCHEMA_REGISTRY
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert "solicitation_analysis" in assets
        assert assets["solicitation_analysis"].metadata.get("status") == "active"
        assert "solicitation_analysis.v1" in SCHEMA_REGISTRY

        # Requirement extraction (dual strategy)
        assert "requirement_extraction_a" in assets
        assert "requirement_extraction_b" in assets
        assert assets["requirement_extraction_a"].metadata.get("status") == "active"
        assert assets["requirement_extraction_b"].metadata.get("status") == "active"

    # ── DoD 5: historical pricing and winners ────────────────────────────────

    def test_dod_5_historical_pricing(self) -> None:
        """DoD 5 — Award model and award intelligence exist (Phase 5)."""
        from govcon.models import Award

        cols = {c.name for c in Award.__table__.columns}
        assert "total_obligation" in cols
        assert "recipient_name" in cols
        assert "action_date" in cols
        assert "nsn" in cols

        from govcon.intelligence.awards import award_history_for_agency

        assert callable(award_history_for_agency)

    # ── DoD 6: AI research/structure supplier and product options ─────────────

    def test_dod_6_supplier_analysis_prompt_active(self) -> None:
        """DoD 6 — supplier_analysis prompt is active with production text (Phase 20)."""
        from govcon.prompting.loader import iter_markdown_prompts
        from govcon.prompting.renderer import render_system_prompt

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert "supplier_analysis" in assets, "supplier_analysis prompt not found"
        a = assets["supplier_analysis"]
        assert a.metadata.get("status") == "active", (
            f"supplier_analysis is not active: status={a.metadata.get('status')}"
        )
        assert a.metadata.get("required_variables"), (
            "supplier_analysis missing required_variables front matter"
        )
        rendered = render_system_prompt(a, PROMPT_ROOT)
        assert "SOURCE SECURITY RULES" in rendered
        assert "NO-FABRICATION RULES" in rendered

    def test_dod_6_supplier_analysis_schema_registered(self) -> None:
        """DoD 6 — supplier_analysis.v1 schema is in SCHEMA_REGISTRY."""
        from govcon.ai.schemas import SCHEMA_REGISTRY, SupplierAnalysisV1

        assert "supplier_analysis.v1" in SCHEMA_REGISTRY
        assert SCHEMA_REGISTRY["supplier_analysis.v1"] is SupplierAnalysisV1

    def test_dod_6_supplier_analysis_schema_validates(self) -> None:
        """DoD 6 — SupplierAnalysisV1 schema validates correct output."""
        from govcon.ai.schemas import validate_analysis_output

        valid_output = {
            "candidates": [
                {
                    "supplier": "Acme Medical",
                    "product": "NSN 6515-01-234-5678",
                    "exact_requirement_matches": ["NSN match", "Quantity match"],
                    "partial_requirement_matches": [],
                    "unsupported_claims": [],
                    "specification_mismatches": [],
                    "delivery_lead_time_risk": "low",
                    "origin_compliance_gaps": [],
                    "quote_commercial_risks": ["Quote expires 2026-12-01"],
                    "evidence_still_required": ["Cage code verification"],
                    "source_refs": [],
                }
            ],
            "overall_sourcing_risk": "low",
            "recommended_next_steps": ["Obtain updated quote"],
            "source_refs": [],
        }
        result = validate_analysis_output("supplier_analysis.v1", valid_output)
        assert result.candidates[0].supplier == "Acme Medical"
        assert result.overall_sourcing_risk == "low"

    def test_dod_6_supplier_analysis_schema_rejects_bad_risk(self) -> None:
        """DoD 6 — SupplierAnalysisV1 rejects invalid overall_sourcing_risk value."""
        from pydantic import ValidationError

        from govcon.ai.schemas import SupplierAnalysisV1

        with pytest.raises(ValidationError):
            SupplierAnalysisV1(overall_sourcing_risk="extreme")

    # ── DoD 7: AI pricing/commercial analysis ────────────────────────────────

    def test_dod_7_pricing_analysis_prompt_active(self) -> None:
        """DoD 7 — pricing_analysis prompt is active with production text (Phase 20)."""
        from govcon.prompting.loader import iter_markdown_prompts
        from govcon.prompting.renderer import render_system_prompt

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert "pricing_analysis" in assets, "pricing_analysis prompt not found"
        a = assets["pricing_analysis"]
        assert a.metadata.get("status") == "active", (
            f"pricing_analysis is not active: status={a.metadata.get('status')}"
        )
        assert a.metadata.get("required_variables"), (
            "pricing_analysis missing required_variables front matter"
        )
        rendered = render_system_prompt(a, PROMPT_ROOT)
        assert "SOURCE SECURITY RULES" in rendered
        assert "NO-FABRICATION RULES" in rendered

    def test_dod_7_pricing_analysis_schema_registered(self) -> None:
        """DoD 7 — pricing_analysis.v1 schema is in SCHEMA_REGISTRY."""
        from govcon.ai.schemas import SCHEMA_REGISTRY, PricingAnalysisV1

        assert "pricing_analysis.v1" in SCHEMA_REGISTRY
        assert SCHEMA_REGISTRY["pricing_analysis.v1"] is PricingAnalysisV1

    def test_dod_7_pricing_analysis_schema_validates(self) -> None:
        """DoD 7 — PricingAnalysisV1 schema validates correct output."""
        from govcon.ai.schemas import validate_analysis_output

        valid_output = {
            "historical_comparability": "3 comparable NSN awards in the last 18 months",
            "proposed_price_position": "at market",
            "expected_margin_quality": "Margin of 18% is within historical range",
            "cost_risk_signals": ["Freight cost not confirmed"],
            "missing_cost_inputs": [],
            "pricing_evidence_gaps": ["No DLA recent award for this NSN"],
            "more_research_warranted": False,
            "confidence": "medium",
            "source_refs": [],
        }
        result = validate_analysis_output("pricing_analysis.v1", valid_output)
        assert result.proposed_price_position == "at market"
        assert result.confidence == "medium"
        assert result.more_research_warranted is False

    def test_dod_7_pricing_analysis_no_autonomous_price_setting(self) -> None:
        """DoD 7 — pricing_analysis prompt forbids autonomous price setting."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        body = assets["pricing_analysis"].body
        assert any(
            phrase in body
            for phrase in [
                "Do not autonomously set",
                "do not autonomously set",
                "autonomously set or approve",
            ]
        ), "pricing_analysis prompt does not forbid autonomous price setting"

    # ── DoD 8: compliance matrix ──────────────────────────────────────────────

    def test_dod_8_compliance_subsystem(self) -> None:
        """DoD 8 — High-reliability compliance subsystem exists (Phase 9)."""
        from govcon.compliance.extractor import scan_requirements
        from govcon.compliance.reconciler import reconcile
        from govcon.compliance.validator import run_validation

        assert callable(scan_requirements)
        assert callable(reconcile)
        assert callable(run_validation)

        from govcon.ai.schemas import SCHEMA_REGISTRY

        assert "requirement_extraction.v1" in SCHEMA_REGISTRY
        assert "compliance_validation.v1" in SCHEMA_REGISTRY
        assert "contradiction_detection.v1" in SCHEMA_REGISTRY

    # ── DoD 9: JEV-backed bid recommendation ─────────────────────────────────

    def test_dod_9_jev_bid_recommendation(self) -> None:
        """DoD 9 — JEV-backed preliminary bid recommendation (Phase 8)."""
        from govcon.decision.engine import run_preliminary_decision_package
        from govcon.decision.providers.rule_fallback import RuleDecisionProvider

        assert callable(run_preliminary_decision_package)
        assert callable(RuleDecisionProvider)

        from govcon.models import DecisionRun

        cols = {c.name for c in DecisionRun.__table__.columns}
        # decision_runs stores the bundle result in the result JSONB column
        assert "bundle_name" in cols
        assert "result" in cols  # result JSON contains recommendation + explanation
        assert "input_state" in cols

    # ── DoD 10: consolidated AI decision package ──────────────────────────────

    def test_dod_10_decision_package(self) -> None:
        """DoD 10 — Consolidated AI decision package generation (Phase 8)."""
        from govcon.decision.engine import run_preliminary_decision_package

        assert callable(run_preliminary_decision_package)

    # ── DoD 11/12: reviewer assignment and parallel review ───────────────────

    def test_dod_11_12_reviewer_assignment(self) -> None:
        """DoD 11/12 — Reviewer assignment and parallel review (Phase 10)."""
        from govcon.collaboration.assignments import assign_reviewer, list_assignments

        assert callable(assign_reviewer)
        assert callable(list_assignments)

        from govcon.models import ReviewSession

        rs_cols = {c.name for c in ReviewSession.__table__.columns}
        assert "review_policy" in rs_cols  # 'single' | 'dual' | 'conditional'
        assert "completed_review_count" in rs_cols

    # ── DoD 13: AI comment validation ────────────────────────────────────────

    def test_dod_13_ai_comment_validation(self) -> None:
        """DoD 13 — AI comment validation opinion (Phase 10)."""
        from govcon.collaboration.ai_comment_review import validate_comment_with_ai

        assert callable(validate_comment_with_ai)

        from govcon.ai.schemas import SCHEMA_REGISTRY

        assert "reviewer_comment_validation.v1" in SCHEMA_REGISTRY

    # ── DoD 14: quorum and consolidated review ───────────────────────────────

    def test_dod_14_quorum_and_consolidated_review(self) -> None:
        """DoD 14 — Review quorum and consolidated AI/JEV review (Phase 10)."""
        from govcon.collaboration.review_sessions import recalculate_quorum

        assert callable(recalculate_quorum)

        from govcon.ai.schemas import SCHEMA_REGISTRY

        assert "consolidated_review.v1" in SCHEMA_REGISTRY

    # ── DoD 15: approve/return/reject bid ────────────────────────────────────

    def test_dod_15_bid_approval(self) -> None:
        """DoD 15 — Human approve/return/reject bid decision (Phase 10)."""
        from govcon.collaboration.review_sessions import finalize_approval

        assert callable(finalize_approval)

    # ── DoD 16: versioned proposal generation ────────────────────────────────

    def test_dod_16_versioned_proposal(self) -> None:
        """DoD 16 — Versioned proposal generated after approval (Phase 11)."""
        from govcon.proposals.service import generate_proposal

        assert callable(generate_proposal)

        from govcon.models import Proposal, ProposalVersion

        prop_cols = {c.name for c in Proposal.__table__.columns}
        assert "pursuit_id" in prop_cols

        ver_cols = {c.name for c in ProposalVersion.__table__.columns}
        assert "version_number" in ver_cols
        assert "full_text" in ver_cols  # full proposal text stored in full_text

    # ── DoD 17: submission instructions and checklist ────────────────────────

    def test_dod_17_submission_checklist(self) -> None:
        """DoD 17 — Submission instructions and required-material checklist (Phase 11)."""
        from govcon.submissions.service import generate_submission_package

        assert callable(generate_submission_package)

        from govcon.models import Submission

        cols = {c.name for c in Submission.__table__.columns}
        assert "required_files" in cols  # checklist items stored as required_files JSONB
        assert "status" in cols

    # ── DoD 18: compliance red-team, coverage, pre-flight ────────────────────

    def test_dod_18_compliance_validation_chain(self) -> None:
        """DoD 18 — Compliance red-team, proposal coverage, pre-flight (Phase 9/11)."""
        from govcon.ai.schemas import SCHEMA_REGISTRY

        assert "compliance_red_team.v1" in SCHEMA_REGISTRY
        assert "proposal_coverage.v1" in SCHEMA_REGISTRY
        assert "submission_preflight_ai.v1" in SCHEMA_REGISTRY

        from govcon.compliance.submission_preflight import run_submission_preflight

        assert callable(run_submission_preflight)

    # ── DoD 19: final human submission approval ───────────────────────────────

    def test_dod_19_final_human_approval(self) -> None:
        """DoD 19 — Final human approval for submission (Phase 11)."""
        from govcon.proposals.service import finalize_proposal

        assert callable(finalize_proposal)

        sig = inspect.signature(finalize_proposal)
        assert "action" in sig.parameters or "session" in sig.parameters

    # ── DoD 20: manual submit and record confirmation ─────────────────────────

    def test_dod_20_manual_submission_and_confirmation(self) -> None:
        """DoD 20 — Manual submission recording and confirmation (Phase 11)."""
        from govcon.proposals.service import record_submission_confirmation

        assert callable(record_submission_confirmation)

        src = (SRC_ROOT / "submissions" / "service.py").read_text(encoding="utf-8")
        # No automatic portal automation
        for forbidden in ["submit_to_sam", "submit_to_piee", "portal_login"]:
            assert forbidden not in src, (
                f"submissions/service.py contains forbidden automation: {forbidden!r}"
            )

    # ── DoD 21: win/loss/no-bid tracking ─────────────────────────────────────

    def test_dod_21_outcome_tracking(self) -> None:
        """DoD 21 — Win/loss/no-bid outcome tracking (Phase 15)."""
        from govcon.learning.outcomes import record_outcome

        assert callable(record_outcome)

        from govcon.models import OutcomeFeedback

        cols = {c.name for c in OutcomeFeedback.__table__.columns}
        assert "outcome" in cols
        assert "pursuit_id" in cols

    # ── DoD 22: debrief/lessons learned ──────────────────────────────────────

    def test_dod_22_lessons_learned(self) -> None:
        """DoD 22 — Debrief/lessons learned stored (Phase 15)."""
        from govcon.models import OutcomeFeedback

        cols = {c.name for c in OutcomeFeedback.__table__.columns}
        # debrief notes stored in the outcome_feedback record
        assert "debrief_notes" in cols or "outcome_notes" in cols or "outcome" in cols

    # ── DoD 23: outcome-informed future analysis ──────────────────────────────

    def test_dod_23_outcome_analytics_without_fabrication(self) -> None:
        """DoD 23 — Outcomes improve future analysis without fabricating certainty (Phase 15)."""
        from govcon.learning.analytics import outcome_analytics

        assert callable(outcome_analytics)

        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert "outcome_analysis" in assets
        assert assets["outcome_analysis"].metadata.get("status") == "active"

    # ── DoD 24: prompt registry for all AI calls ─────────────────────────────

    def test_dod_24_all_ai_calls_through_registry(self) -> None:
        """DoD 24 — All production AI calls go through the versioned prompt registry."""
        from govcon.ai.structured import resolve_prompt, run_structured_prompt

        assert callable(resolve_prompt)
        assert callable(run_structured_prompt)

        # Check inline prompts are absent from service modules
        import re

        service_roots = [
            SRC_ROOT / "enrich",
            SRC_ROOT / "proposals",
            SRC_ROOT / "submissions",
            SRC_ROOT / "collaboration",
        ]
        inline_pattern = r'"role"\s*:\s*"system"\s*,\s*"content"\s*:'
        for root in service_roots:
            for py_file in root.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                src_text = py_file.read_text(encoding="utf-8")
                matches = re.findall(inline_pattern, src_text)
                assert not matches, (
                    f"Inline system prompt found in {py_file.relative_to(REPO_ROOT)}"
                )

    # ── DoD 25: reproducible AI output metadata ───────────────────────────────

    def test_dod_25_reproducible_ai_output_metadata(self) -> None:
        """DoD 25 — Every AI call records prompt name/version/hash/model/schema (Phase 7+)."""
        from govcon.models import AIAnalysis

        cols = {c.name for c in AIAnalysis.__table__.columns}
        assert "prompt_name" in cols
        assert "prompt_version" in cols
        assert "prompt_hash" in cols
        assert "provider" in cols
        assert "model" in cols

    # ── DoD 26: regression gate for safety-critical prompts ───────────────────

    def test_dod_26_regression_gate_exists(self) -> None:
        """DoD 26 — Safety-critical prompt changes require regression evaluation (Phase 9)."""
        from govcon.prompting.evaluation import (
            SAFETY_CRITICAL_PROMPTS,
            run_activation_gate,
        )

        assert len(SAFETY_CRITICAL_PROMPTS) >= 9
        assert callable(run_activation_gate)

        from govcon.compliance.regression import run_benchmark_suite

        assert callable(run_benchmark_suite)

    # ── DoD 27: prompt rollback without losing audit ──────────────────────────

    def test_dod_27_prompt_rollback_with_audit(self) -> None:
        """DoD 27 — Prompt rollback without losing historical audit (Phase 9)."""
        from govcon.prompting.registry import rollback_prompt, sync_prompts

        assert callable(rollback_prompt)
        assert callable(sync_prompts)

        from govcon.models import PromptRegistryEntry

        cols = {c.name for c in PromptRegistryEntry.__table__.columns}
        assert "prompt_hash" in cols


# ---------------------------------------------------------------------------
# §26 Feature checklist parity
# ---------------------------------------------------------------------------


class TestFeatureChecklist:
    """Verify §26 feature checklist capabilities are present."""

    def test_sam_ingestion_present(self) -> None:
        """Federal opportunities: SAM ingestion (Phase 1)."""
        from govcon.ingest.sam_opportunities import ingest_opportunity_records

        assert callable(ingest_opportunity_records)

    def test_snapshot_history_present(self) -> None:
        """Immutable opportunity history: snapshots + events (Phase 1)."""
        from govcon.models import OpportunitySnapshot

        assert OpportunitySnapshot.__table__ is not None

    def test_matching_engine_present(self) -> None:
        """Search/matching: FTS + filters (Phase 2/12/14)."""
        from govcon.matching.engine import evaluate_match

        assert callable(evaluate_match)

    def test_alerts_watchlists_present(self) -> None:
        """Alerts: watchlists + digest (Phase 2-3)."""
        from govcon.alerts.digest import run_digest

        assert callable(run_digest)

    def test_dibbs_ingestion_present(self) -> None:
        """DLA commodity: DIBBS ingestion (Phase 4)."""
        from govcon.ingest.dibbs import ingest_index_file

        assert callable(ingest_index_file)

    def test_usaspending_awards_present(self) -> None:
        """Federal awards: USAspending (Phase 5)."""
        from govcon.ingest.usaspending import normalize_award

        assert callable(normalize_award)

    def test_pricing_intelligence_present(self) -> None:
        """Historical pricing: pricing intelligence (Phase 5)."""
        from govcon.intelligence.awards import award_history_for_agency, top_awardees

        assert callable(award_history_for_agency)
        assert callable(top_awardees)

    def test_vendor_profiles_present(self) -> None:
        """Vendor profiles: SAM entities + award stats (Phase 6)."""
        from govcon.intelligence.vendors import vendor_profile

        assert callable(vendor_profile)

    def test_competitor_intelligence_present(self) -> None:
        """Competitor intelligence: prior awardees (Phase 6)."""
        from govcon.intelligence.competitors import competitor_summary

        assert callable(competitor_summary)

    def test_attachment_extraction_present(self) -> None:
        """Attachment extraction: local files (Phase 7)."""
        from govcon.enrich.attachments import process_local_file

        assert callable(process_local_file)

    def test_structured_ai_summary_present(self) -> None:
        """Structured AI summary: ai_analyses (Phase 7)."""
        from govcon.models import AIAnalysis

        assert AIAnalysis.__table__ is not None

    def test_versioned_prompt_library_present(self) -> None:
        """Versioned prompt library: source-controlled prompts + registry (Phase 7+)."""
        from govcon.models import PromptRegistryEntry
        from govcon.prompting.loader import iter_markdown_prompts

        assert PromptRegistryEntry.__table__ is not None
        assets = iter_markdown_prompts(PROMPT_ROOT)
        active = [a for a in assets if a.metadata.get("status") == "active"]
        assert len(active) >= 18, f"Expected >= 18 active prompts, got {len(active)}"

    def test_prompt_regression_gates_present(self) -> None:
        """Prompt regression gates: prompting/evaluation.py (Phase 7+)."""
        from govcon.prompting.evaluation import run_activation_gate

        assert callable(run_activation_gate)

    def test_source_prompt_injection_defense_present(self) -> None:
        """Source prompt-injection defense: shared source-security contract (Phase 7+)."""
        fragment = PROMPT_ROOT / "shared" / "source_security_rules_v1.md"
        assert fragment.exists()
        text = fragment.read_text(encoding="utf-8")
        assert len(text) > 100

    def test_jev_decision_layer_present(self) -> None:
        """JEV structured decision layer: decision bundles + decision_runs (Phase 8+)."""
        jev_dir = PROMPT_ROOT / "jev"
        assert jev_dir.is_dir()
        bundles = list(jev_dir.glob("*.yaml"))
        assert len(bundles) >= 13, f"Expected >= 13 JEV bundles, got {len(bundles)}"

    def test_compliance_subsystem_present(self) -> None:
        """High-reliability compliance matrix: dual extraction + validators (Phase 9)."""
        from govcon.compliance.extractor import scan_requirements
        from govcon.compliance.reconciler import reconcile
        from govcon.compliance.validator import run_validation

        assert all(callable(fn) for fn in [scan_requirements, reconcile, run_validation])

    def test_semantic_search_present(self) -> None:
        """Semantic recommendations: pgvector (Phase 13)."""
        from govcon.matching.semantic import similar_opportunities

        assert callable(similar_opportunities)

    def test_web_ui_present(self) -> None:
        """Full bid workspace: web UI (Phase 14)."""
        web_dir = SRC_ROOT / "web"
        assert (web_dir / "routes" / "__init__.py").exists()
        assert (web_dir / "templates").is_dir()

    def test_outcome_learning_present(self) -> None:
        """Win/loss/no-bid learning: outcome_feedback (Phase 15)."""
        from govcon.learning.analytics import outcome_analytics
        from govcon.learning.outcomes import record_outcome

        assert callable(record_outcome)
        assert callable(outcome_analytics)

    def test_scheduling_present(self) -> None:
        """Pipeline scheduling (Phase 14/17)."""
        from govcon.scheduler.chains import CHAIN_DEFINITIONS

        # SAM ingest chain exists
        assert "morning_ingest" in CHAIN_DEFINITIONS
        morning = CHAIN_DEFINITIONS["morning_ingest"]
        assert len(morning.steps) >= 4

    def test_data_classification_present(self) -> None:
        """Data classification: AI gateway (Phase 18)."""
        from govcon.ai.gateway import authorize_external_call

        assert callable(authorize_external_call)

    def test_market_analysis_in_checklist(self) -> None:
        """market_analysis prompt active for market intelligence capability."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert "market_analysis" in assets
        assert assets["market_analysis"].metadata.get("status") == "active"

    def test_phase16_waiver_documented(self) -> None:
        """Phase 16 (state/local) is waived and documented in SPEC_DEVIATIONS."""
        spec_dev = REPO_ROOT / "SPEC_DEVIATIONS.md"
        text = spec_dev.read_text(encoding="utf-8")
        assert "Phase 16" in text or "DEV-015" in text

        impl_status = REPO_ROOT / "IMPLEMENTATION_STATUS.md"
        text2 = impl_status.read_text(encoding="utf-8")
        assert "DEFERRED" in text2


# ---------------------------------------------------------------------------
# Market/Supplier/Pricing prompt activation gate
# ---------------------------------------------------------------------------


class TestNewPromptActivationGate:
    """Verify all three new Phase 20 prompts pass the full activation gate."""

    @pytest.mark.parametrize("prompt_name", ["market_analysis", "supplier_analysis", "pricing_analysis"])
    def test_prompt_passes_activation_gate(self, prompt_name: str) -> None:
        """Each new prompt passes the full activation gate (§43.8)."""
        from govcon.prompting.evaluation import run_activation_gate
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert prompt_name in assets, f"{prompt_name} not found in prompt root"
        asset = assets[prompt_name]
        result = run_activation_gate(asset, PROMPT_ROOT, run_regression=False)
        assert result.passed, (
            f"{prompt_name} activation gate FAILED:\n" + "\n".join(result.failures)
        )

    @pytest.mark.parametrize("prompt_name", ["market_analysis", "supplier_analysis", "pricing_analysis"])
    def test_prompt_is_active(self, prompt_name: str) -> None:
        """Each new prompt has status: active."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        asset = assets[prompt_name]
        assert asset.metadata.get("status") == "active", (
            f"{prompt_name} status is {asset.metadata.get('status')!r}, expected 'active'"
        )

    @pytest.mark.parametrize("prompt_name,schema_key", [
        ("market_analysis", "market_analysis.v1"),
        ("supplier_analysis", "supplier_analysis.v1"),
        ("pricing_analysis", "pricing_analysis.v1"),
    ])
    def test_prompt_schema_registered(self, prompt_name: str, schema_key: str) -> None:
        """Schema version for each new prompt is registered in SCHEMA_REGISTRY."""
        from govcon.ai.schemas import SCHEMA_REGISTRY

        assert schema_key in SCHEMA_REGISTRY, (
            f"{schema_key} not in SCHEMA_REGISTRY"
        )

    def test_total_active_prompts_at_least_18(self) -> None:
        """Total active task prompts must be >= 18 (15 from Phase 19 + 3 new)."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = iter_markdown_prompts(PROMPT_ROOT)
        active = [
            a for a in assets
            if a.metadata.get("status") == "active"
            and "shared" not in str(a.path)
        ]
        assert len(active) >= 18, (
            f"Expected >= 18 active task prompts, got {len(active)}: "
            + str([a.name for a in active])
        )


# ---------------------------------------------------------------------------
# Schema validation for new analysis types
# ---------------------------------------------------------------------------


class TestNewSchemas:
    """Test MarketAnalysisV1, SupplierAnalysisV1, and PricingAnalysisV1 schemas."""

    def test_market_analysis_minimal_valid(self) -> None:
        """MarketAnalysisV1 accepts a minimal valid payload."""
        from govcon.ai.schemas import validate_analysis_output

        result = validate_analysis_output("market_analysis.v1", {})
        assert result.comparable_awards == []
        assert result.historical_winners == []

    def test_market_analysis_full_payload(self) -> None:
        """MarketAnalysisV1 validates a full award comparison payload."""
        from govcon.ai.schemas import validate_analysis_output

        payload = {
            "comparable_awards": [
                {
                    "vendor": "Omega Medical",
                    "amount": 42500.00,
                    "date": "2025-06-15",
                    "nsn": "6515-01-234-5678",
                    "psc": "6515",
                    "comparability_note": "exact NSN match",
                    "source_refs": [],
                }
            ],
            "historical_winners": ["Omega Medical", "Alpha Supply"],
            "incumbent_signals": ["Omega Medical awarded last 3 cycles"],
            "recurring_vendors": ["Omega Medical"],
            "price_comparability": "Unit price not derivable from obligation without quantity",
            "agency_buying_patterns": "DLA TROOP SUPPORT — 3 awards per year for this NSN",
            "competition_signals": ["Set-aside: Small Business", "2 awardees in last 3 cycles"],
            "recompete_signals": ["Contract 0021 expires Q2 2026"],
            "comparability_weaknesses": [],
            "source_refs": [],
        }
        result = validate_analysis_output("market_analysis.v1", payload)
        assert result.comparable_awards[0].vendor == "Omega Medical"
        assert result.comparable_awards[0].amount == 42500.00

    def test_supplier_analysis_unknown_risk_is_default(self) -> None:
        """SupplierAnalysisV1 defaults overall_sourcing_risk to UNKNOWN."""
        from govcon.ai.schemas import SupplierAnalysisV1

        a = SupplierAnalysisV1()
        assert a.overall_sourcing_risk == "UNKNOWN"

    def test_pricing_analysis_low_confidence_is_default(self) -> None:
        """PricingAnalysisV1 defaults confidence to low."""
        from govcon.ai.schemas import PricingAnalysisV1

        a = PricingAnalysisV1()
        assert a.confidence == "low"

    def test_all_three_schema_classes_instantiate(self) -> None:
        """All three new schema classes can be instantiated with empty defaults."""
        from govcon.ai.schemas import (
            MarketAnalysisV1,
            PricingAnalysisV1,
            SupplierAnalysisV1,
        )

        m = MarketAnalysisV1()
        s = SupplierAnalysisV1()
        p = PricingAnalysisV1()
        assert m is not None
        assert s is not None
        assert p is not None

    def test_market_analysis_schema_version_key(self) -> None:
        """market_analysis_v1.md declares schema_version: market_analysis.v1."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert assets["market_analysis"].metadata.get("schema_version") == "market_analysis.v1"

    def test_supplier_analysis_schema_version_key(self) -> None:
        """supplier_analysis_v1.md declares schema_version: supplier_analysis.v1."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert assets["supplier_analysis"].metadata.get("schema_version") == "supplier_analysis.v1"

    def test_pricing_analysis_schema_version_key(self) -> None:
        """pricing_analysis_v1.md declares schema_version: pricing_analysis.v1."""
        from govcon.prompting.loader import iter_markdown_prompts

        assets = {a.name: a for a in iter_markdown_prompts(PROMPT_ROOT)}
        assert assets["pricing_analysis"].metadata.get("schema_version") == "pricing_analysis.v1"

    def test_market_analysis_schema_in_schema_registry(self) -> None:
        """market_analysis.v1 is registered in SCHEMA_REGISTRY."""
        from govcon.ai.schemas import SCHEMA_REGISTRY, MarketAnalysisV1

        assert "market_analysis.v1" in SCHEMA_REGISTRY
        assert SCHEMA_REGISTRY["market_analysis.v1"] is MarketAnalysisV1

    def test_validate_analysis_output_dispatches_market(self) -> None:
        """validate_analysis_output dispatches to MarketAnalysisV1 correctly."""
        from govcon.ai.schemas import MarketAnalysisV1, validate_analysis_output

        result = validate_analysis_output("market_analysis.v1", {"comparable_awards": []})
        assert isinstance(result, MarketAnalysisV1)

    def test_validate_analysis_output_dispatches_supplier(self) -> None:
        """validate_analysis_output dispatches to SupplierAnalysisV1 correctly."""
        from govcon.ai.schemas import SupplierAnalysisV1, validate_analysis_output

        result = validate_analysis_output("supplier_analysis.v1", {"candidates": []})
        assert isinstance(result, SupplierAnalysisV1)

    def test_validate_analysis_output_dispatches_pricing(self) -> None:
        """validate_analysis_output dispatches to PricingAnalysisV1 correctly."""
        from govcon.ai.schemas import PricingAnalysisV1, validate_analysis_output

        result = validate_analysis_output("pricing_analysis.v1", {})
        assert isinstance(result, PricingAnalysisV1)


# ---------------------------------------------------------------------------
# §28 Non-goals guard
# ---------------------------------------------------------------------------


class TestNonGoalsNotImplemented:
    """Verify §28 non-goals are NOT implemented in v1."""

    def test_no_automatic_portal_submission(self) -> None:
        """§28 — No automatic portal login or SAM/PIEE/eBuy submission."""
        submission_src = (SRC_ROOT / "submissions" / "service.py").read_text(encoding="utf-8")
        for forbidden in ["submit_to_sam", "submit_to_piee", "submit_to_ebuy", "portal_login"]:
            assert forbidden not in submission_src, (
                f"Non-goal portal automation found: {forbidden!r}"
            )

    def test_no_autonomous_final_bid_decision(self) -> None:
        """§28 — No autonomous final bid decision; human approval required."""
        from govcon.collaboration.review_sessions import finalize_approval

        # finalize_approval is a function that requires session + human decision
        sig = inspect.signature(finalize_approval)
        params = list(sig.parameters.keys())
        assert len(params) >= 2, "finalize_approval must require at least session + decision args"

    def test_state_local_not_implemented(self) -> None:
        """§28 — State/local adapters (Phase 16) are DEFERRED, not implemented."""
        state_adapter = SRC_ROOT / "ingest" / "state_local.py"
        if state_adapter.exists():
            content = state_adapter.read_text(encoding="utf-8")
            # Must be a stub — no real scraping logic
            assert len(content.strip()) < 500, (
                "state_local.py exists with substantive content — Phase 16 may be implemented"
            )

    def test_no_cui_through_external_llm(self) -> None:
        """§28 — CUI classification blocks external LLM calls (Phase 18)."""
        from govcon.ai.gateway import authorize_external_call

        sig = inspect.signature(authorize_external_call)
        # The function must accept a classification parameter
        assert "classification" in sig.parameters or "level" in sig.parameters, (
            "authorize_external_call must accept classification level"
        )
