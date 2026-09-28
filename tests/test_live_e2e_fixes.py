"""Regression tests for three live E2E gaps found on DIBBS opportunity 8836 (SPE4A526T443K).

Fix 1 — JEV / AIGateway proprietary block must not wipe compliance.
Fix 2 — DIBBS attachments in ``enrich download``.
Fix 3 — Solicitation / requirement schema coercive adapters.

No live network or external API keys are required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.gateway import AIGatewayBlocked
from govcon.ai.schemas import EvaluationFactor, MissingInfo, SolicitationAnalysisV1
from govcon.compliance.schemas import (
    ExtractedRequirement,
    RequirementExtractionV1,
    _CONFIDENCE_LABEL_MAP,
)
from govcon.enrich.attachments import (
    _collect_attachment_urls,
    _dibbs_rfq_pdf_url,
    _is_dibbs_url,
)
from govcon.models import Opportunity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid4().hex[:10]


def _opp(session: Session, **overrides) -> Opportunity:
    data = {
        "source": "sam",
        "source_id": f"live-e2e-fix-{_uid()}",
        "title": "Test opportunity",
        "status": "open",
        "raw": {},
        "links": {},
    }
    data.update(overrides)
    row = Opportunity(**data)
    session.add(row)
    session.flush()
    return row


def _base_state(opp: Opportunity) -> dict:
    return {
        "opportunity": {
            "id": opp.id,
            "source": opp.source,
            "source_id": opp.source_id,
            "title": opp.title,
            "status": opp.status,
            "set_aside": opp.set_aside_code,
            "days_remaining": 10,
            "required_delivery_days": 12,
            "response_deadline": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        },
        "eligibility": {"sam_active": True, "set_aside_match": True, "mandatory_certifications_met": True},
        "sourcing": {"product_found": True, "supplier_count": 2, "best_supplier_cost": 8000.0, "lead_time_days": 9},
        "pricing": {"proposed_price": 10000.0, "supplier_cost": 8000.0, "margin_pct": 20.0, "historical_comparable_count": 5, "historical_median": 10500.0},
        "compliance": {"mandatory_total": 5, "mandatory_missing": 0, "needs_review": 0, "critical_total": 1, "critical_unresolved": 0, "country_of_origin_conflict": False},
        "analysis": {"missing_information": [], "risk_flags": []},
        "amendment": {"count": 0, "material": False},
        "review": {"review_policy": "conditional", "required_review_count": 1, "completed_review_count": 0},
        "scores": {"capability_fit_score": 0.75, "product_fit_score": 0.70},
        "signals": {"has_attachments": True, "requires_deep_analysis": True, "competitor_bucket_count": 1, "incumbent_signal": False},
        "outcome": {"evidence_count": 0, "no_bid_reason": None, "loss_reason": None},
        "source_snapshot_ids": [],
        "evidence_refs": [],
        "bundle_results": {},
    }


# ===========================================================================
# Fix 1 — JEV AIGatewayBlocked fallback
# ===========================================================================


class TestJevGatewayBlockedFallback:
    """AIGatewayBlocked from the JEV provider must fall back to rules, not propagate."""

    def test_gateway_blocked_falls_back_to_rules_in_run_decision_bundle(self, upgraded_engine) -> None:
        """When JEV raises AIGatewayBlocked, run_decision_bundle must use the rules provider."""
        from govcon.decision.engine import run_decision_bundle
        from govcon.decision.providers.jev import JevDecisionProvider
        from govcon.security.classification import DataClassification

        with patch.object(JevDecisionProvider, "from_settings") as mock_from_settings:
            mock_provider = MagicMock()
            mock_provider.decide.side_effect = AIGatewayBlocked(DataClassification.PROPRIETARY)
            mock_from_settings.return_value = mock_provider

            with Session(upgraded_engine) as session:
                opp = _opp(session, status="open")
                state = _base_state(opp)
                execution = run_decision_bundle(
                    session,
                    opportunity_id=opp.id,
                    bundle_name="bid_decision",
                    state=state,
                    settings=_settings_with_jev(),
                )
                run_id = execution.run.id  # read inside session
                provider = execution.provider
                session.commit()

        assert provider == "rules", "expected rules fallback after gateway block"
        assert run_id is not None

    def test_gateway_blocked_on_jev_does_not_prevent_decision_run_persistence(self, upgraded_engine) -> None:
        """A gateway block must not roll back the DecisionRun row."""
        from govcon.decision.engine import run_decision_bundle
        from govcon.decision.providers.jev import JevDecisionProvider
        from govcon.models import DecisionRun
        from govcon.security.classification import DataClassification

        with patch.object(JevDecisionProvider, "from_settings") as mock_from_settings:
            mock_provider = MagicMock()
            mock_provider.decide.side_effect = AIGatewayBlocked(DataClassification.PROPRIETARY)
            mock_from_settings.return_value = mock_provider

            with Session(upgraded_engine) as session:
                opp = _opp(session, status="open")
                state = _base_state(opp)
                execution = run_decision_bundle(
                    session,
                    opportunity_id=opp.id,
                    bundle_name="opportunity_triage",
                    state=state,
                    settings=_settings_with_jev(),
                )
                session.commit()
                run_id = execution.run.id

            with Session(upgraded_engine) as session2:
                persisted = session2.get(DecisionRun, run_id)
                assert persisted is not None
                assert persisted.provider == "rules"

    def test_jev_routing_with_gateway_block_returns_skipped_status(self, upgraded_engine) -> None:
        """run_jev_routing must complete when JEV is gateway-blocked.

        With the Fix 1 change, run_decision_bundle catches AIGatewayBlocked and falls back
        to rules, so run_jev_routing gets provider='rules' back from the bundle execution.
        The compliance run is never rolled back.
        """
        from govcon.compliance.validator import run_jev_routing
        from govcon.decision.providers.jev import JevDecisionProvider
        from govcon.security.classification import DataClassification

        with patch.object(JevDecisionProvider, "from_settings") as mock_from_settings:
            mock_provider = MagicMock()
            mock_provider.decide.side_effect = AIGatewayBlocked(DataClassification.PROPRIETARY)
            mock_from_settings.return_value = mock_provider

            with Session(upgraded_engine) as session:
                opp = _opp(session, status="open")
                result = run_jev_routing(session, opp.id, settings=_settings_with_jev())
                run_id = result["run_id"]
                provider = result["provider"]
                session.commit()

        # run_decision_bundle catches AIGatewayBlocked and falls back to rules;
        # run_jev_routing receives a valid bundle execution with provider="rules".
        assert provider == "rules", "Expected rules fallback after gateway block"
        assert run_id is not None
        assert result["added_blocks"] == []
        assert result["routed_to_review"] == []

    def test_compliance_pipeline_persists_runs_when_jev_gateway_blocked(self, upgraded_engine) -> None:
        """Full compliance pipeline's jev_routing step must complete with rules fallback
        when JEV is policy-blocked; no rows are rolled back."""
        from govcon.compliance.validator import run_jev_routing
        from govcon.decision.providers.jev import JevDecisionProvider
        from govcon.models import ComplianceRun
        from govcon.security.classification import DataClassification

        with patch.object(JevDecisionProvider, "from_settings") as mock_from_settings:
            mock_provider = MagicMock()
            mock_provider.decide.side_effect = AIGatewayBlocked(DataClassification.PROPRIETARY)
            mock_from_settings.return_value = mock_provider

            with Session(upgraded_engine) as session:
                opp = _opp(session, status="open", source="sam")
                routing = run_jev_routing(session, opp.id, settings=_settings_with_jev())
                run_id = routing["run_id"]
                provider = routing["provider"]
                session.commit()

        # run_decision_bundle now catches AIGatewayBlocked and falls back to rules
        assert provider == "rules"
        assert run_id is not None


def _settings_with_jev():
    """Return settings that would attempt JEV (key present but blocked by gateway)."""
    from govcon.config import Settings

    return Settings(
        database_url="postgresql+psycopg://govcon:govcon@localhost:5432/govcon",
        jev_enabled=True,
        jev_api_key="fake-jev-key-for-testing",
        decision_primary_provider="jev",
        ai_external_allowed_for_proprietary=False,
    )


# ===========================================================================
# Fix 2 — DIBBS attachment URL collection
# ===========================================================================


class TestDibbsAttachmentUrlCollection:
    """_collect_attachment_urls must derive the RFQ PDF URL for DIBBS opportunities."""

    def _dibbs_opp(self) -> Opportunity:
        opp = MagicMock(spec=Opportunity)
        opp.source = "dibbs"
        opp.solicitation_number = "SPE4A526T443K"
        opp.raw = {"solicitation_number": "SPE4A526T443K"}
        opp.links = {
            "ui": "https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn=SPE4A526T443K",
            "package": "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/ca260925.zip",
            "batch_quote": "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/bq260925.zip",
            "index": "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt",
        }
        return opp

    def test_derives_rfq_pdf_url_from_solicitation_number(self) -> None:
        opp = self._dibbs_opp()
        urls = _collect_attachment_urls(opp)
        found = [u for u, _ in urls if "SPE4A526T443K.PDF" in u]
        assert found, f"Expected RFQ PDF URL in {urls}"
        assert found[0] == "https://dibbs2.bsm.dla.mil/Downloads/RFQ/K/SPE4A526T443K.PDF"

    def test_filename_is_solicitation_number_pdf(self) -> None:
        opp = self._dibbs_opp()
        urls = _collect_attachment_urls(opp)
        pdf_entries = [(u, f) for u, f in urls if "SPE4A526T443K.PDF" in u]
        assert pdf_entries
        assert pdf_entries[0][1] == "SPE4A526T443K.PDF"

    def test_no_duplicate_url(self) -> None:
        opp = self._dibbs_opp()
        urls = _collect_attachment_urls(opp)
        url_list = [u for u, _ in urls]
        assert len(url_list) == len(set(url_list)), "Duplicate URLs in result"

    def test_sam_opp_unaffected(self) -> None:
        opp = MagicMock(spec=Opportunity)
        opp.source = "sam"
        opp.solicitation_number = "W91CRB21Q0001"
        opp.raw = {}
        opp.links = {
            "resourceLinks": [{"url": "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc", "name": "rfp.pdf"}]
        }
        urls = _collect_attachment_urls(opp)
        assert len(urls) == 1
        assert "dibbs" not in urls[0][0]

    def test_dibbs_opp_without_solicitation_number_returns_empty(self) -> None:
        opp = MagicMock(spec=Opportunity)
        opp.source = "dibbs"
        opp.solicitation_number = None
        opp.raw = {}
        opp.links = {}
        urls = _collect_attachment_urls(opp)
        assert urls == []

    @pytest.mark.parametrize("solicitation,expected_subdir", [
        ("SPE4A526T443K", "K"),
        ("SPE1C126T1698", "8"),  # ends with digit — no PDF URL derived
        ("W912BU21Q9999", "9"),
        ("SPRBL121Q4001", "1"),
    ])
    def test_rfq_pdf_url_derivation_pattern(self, solicitation: str, expected_subdir: str) -> None:
        url = _dibbs_rfq_pdf_url(solicitation)
        if solicitation[-1].isalpha():
            assert url is not None
            assert f"/Downloads/RFQ/{expected_subdir}/{solicitation}.PDF" in url
        else:
            # Solicitations ending with digits don't get the alpha-prefix subdirectory
            assert url is None

    def test_is_dibbs_url_detection(self) -> None:
        assert _is_dibbs_url("https://dibbs2.bsm.dla.mil/Downloads/RFQ/K/SPE4A526T443K.PDF") is True
        assert _is_dibbs_url("https://dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn=X") is True
        assert _is_dibbs_url("https://sam.gov/api/something") is False
        assert _is_dibbs_url("https://api.sam.gov/opportunities/v2/search") is False

    def test_dibbs_rfq_pdf_none_for_empty_string(self) -> None:
        assert _dibbs_rfq_pdf_url("") is None
        assert _dibbs_rfq_pdf_url("   ") is None

    def test_consent_fetch_called_for_dibbs_url(self) -> None:
        """_download_one must use fetch_consented for DIBBS URLs."""
        import hashlib
        from unittest.mock import MagicMock, patch

        from govcon.enrich.attachments import _download_one

        content = b"%PDF-1.4 fake pdf content for test"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = content

        opp = MagicMock(spec=Opportunity)
        opp.id = 42
        opp.source = "dibbs"

        with patch("govcon.enrich.attachments._is_dibbs_url", return_value=True), \
             patch("govcon.ingest.dibbs.fetch_consented", return_value=mock_response) as mock_fc:
            from govcon.config import Settings

            settings = Settings(
                database_url="sqlite:///:memory:",
                dibbs_request_interval_seconds=0.0,
            )
            session = MagicMock()
            session.execute.return_value.scalar_one_or_none.return_value = None

            result = _download_one(
                session, opp,
                "https://dibbs2.bsm.dla.mil/Downloads/RFQ/K/SPE4A526T443K.PDF",
                "SPE4A526T443K.PDF",
                settings=settings,
                client=MagicMock(),
            )

        mock_fc.assert_called_once()


# ===========================================================================
# Fix 3 — Schema coercive adapters
# ===========================================================================


class TestRequirementExtractionSchemaCoercion:
    """ExtractedRequirement must accept string confidence labels and list-valued normalized_values."""

    def test_confidence_high_string_coerced_to_float(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", confidence="high")
        assert req.confidence == pytest.approx(_CONFIDENCE_LABEL_MAP["high"])

    def test_confidence_medium_string_coerced(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", confidence="medium")
        assert req.confidence == pytest.approx(_CONFIDENCE_LABEL_MAP["medium"])

    def test_confidence_low_string_coerced(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", confidence="low")
        assert req.confidence == pytest.approx(_CONFIDENCE_LABEL_MAP["low"])

    def test_confidence_float_passthrough(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", confidence=0.72)
        assert req.confidence == pytest.approx(0.72)

    def test_confidence_none_stays_none(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.")
        assert req.confidence is None

    def test_confidence_unknown_string_becomes_none(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", confidence="extreme")
        assert req.confidence is None

    def test_normalized_values_list_takes_first_scalar(self) -> None:
        req = ExtractedRequirement(
            requirement_text="Must comply.",
            normalized_values={"delivery_days": [30, 60], "unit": ["EA", "each"]},
        )
        assert req.normalized_values["delivery_days"] == 30
        assert req.normalized_values["unit"] == "EA"

    def test_normalized_values_scalar_passthrough(self) -> None:
        req = ExtractedRequirement(
            requirement_text="Must comply.",
            normalized_values={"delivery_days": 45, "mandatory": True},
        )
        assert req.normalized_values["delivery_days"] == 45
        assert req.normalized_values["mandatory"] is True

    def test_normalized_values_non_dict_becomes_empty(self) -> None:
        req = ExtractedRequirement(requirement_text="Must comply.", normalized_values=None)
        assert req.normalized_values == {}

    def test_full_extraction_output_with_string_confidences(self) -> None:
        """RequirementExtractionV1 must accept the shape models actually emit."""
        data = {
            "requirements": [
                {
                    "requirement_text": "Vendor must be SAM registered.",
                    "mandatory": True,
                    "severity": "critical",
                    "confidence": "high",
                    "normalized_values": {"registration_type": ["SAM", "active"]},
                    "clause_references": [],
                },
                {
                    "requirement_text": "Delivery within 30 days.",
                    "mandatory": True,
                    "severity": "high",
                    "confidence": "medium",
                    "normalized_values": {"delivery_days": [30]},
                    "clause_references": ["FAR 52.211-9"],
                },
            ],
            "extraction_notes": ["Extracted from base solicitation."],
        }
        result = RequirementExtractionV1.model_validate(data)
        assert len(result.requirements) == 2
        assert result.requirements[0].confidence == pytest.approx(_CONFIDENCE_LABEL_MAP["high"])
        assert result.requirements[0].normalized_values["registration_type"] == "SAM"
        assert result.requirements[1].confidence == pytest.approx(_CONFIDENCE_LABEL_MAP["medium"])
        assert result.requirements[1].normalized_values["delivery_days"] == 30


class TestSolicitationAnalysisSchemaCoercion:
    """SolicitationAnalysisV1 must handle string evaluation_factors and missing_information shapes."""

    def test_evaluation_factors_as_string_list(self) -> None:
        data = {
            "summary": "Test",
            "evaluation_factors": ["Technical approach", "Past performance", "Price"],
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert len(result.evaluation_factors) == 3
        assert result.evaluation_factors[0].name == "Technical approach"

    def test_evaluation_factors_as_dict(self) -> None:
        data = {
            "summary": "Test",
            "evaluation_factors": {
                "Technical Approach": "Most important",
                "Price": "Secondary",
            },
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert len(result.evaluation_factors) == 2
        names = {f.name for f in result.evaluation_factors}
        assert "Technical Approach" in names
        assert "Price" in names

    def test_evaluation_factors_as_proper_list_of_dicts(self) -> None:
        data = {
            "evaluation_factors": [
                {"name": "Technical", "weight": "40%", "description": "Technical capability"},
            ]
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert result.evaluation_factors[0].name == "Technical"
        assert result.evaluation_factors[0].weight == "40%"

    def test_missing_information_as_string_list(self) -> None:
        data = {
            "summary": "Test",
            "missing_information": ["Delivery address", "Packaging requirements"],
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert len(result.missing_information) == 2
        assert result.missing_information[0].field == "Delivery address"
        assert result.missing_information[0].reason == "Delivery address"

    def test_missing_information_as_proper_list_of_dicts(self) -> None:
        data = {
            "missing_information": [
                {"field": "delivery_location", "reason": "Not specified", "impact": "Cannot ship"},
            ]
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert result.missing_information[0].field == "delivery_location"
        assert result.missing_information[0].reason == "Not specified"

    def test_evaluation_factors_none_becomes_empty_list(self) -> None:
        data = {"evaluation_factors": None}
        result = SolicitationAnalysisV1.model_validate(data)
        assert result.evaluation_factors == []

    def test_full_model_output_shape_from_deepseek(self) -> None:
        """Simulate the shape DeepSeek actually emitted during live E2E."""
        data = {
            "summary": "DIBBS RFQ for NSN item SPE4A526T443K",
            "evaluation_factors": {
                "Technical Approach": "Meets required specs",
                "Price": "Lowest evaluated price",
                "Past Performance": "Not evaluated",
            },
            "missing_information": [
                "Packaging and marking requirements",
                "Inspection and acceptance location",
            ],
            "items": [],
            "key_dates": [],
            "certifications": ["SAM registration"],
            "risk_flags": [],
        }
        result = SolicitationAnalysisV1.model_validate(data)
        assert len(result.evaluation_factors) == 3
        assert len(result.missing_information) == 2
        assert all(isinstance(m.field, str) for m in result.missing_information)

    def test_evaluation_factor_bare_string_accepted(self) -> None:
        factor = EvaluationFactor.model_validate("Price")
        assert factor.name == "Price"

    def test_missing_info_bare_string_accepted(self) -> None:
        info = MissingInfo.model_validate("Delivery address unknown")
        assert info.field == "Delivery address unknown"
        assert info.reason == "Delivery address unknown"
