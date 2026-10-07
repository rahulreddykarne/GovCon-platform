"""Attachment identity, classification, prompt setup, and sparse model output."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.gateway import AIGatewayBlocked, external_call_allowed
from govcon.ai.providers.deepseek import DeepSeekProvider
from govcon.ai.quality import assess_output_quality
from govcon.ai.schemas import SolicitationAnalysisV1
from govcon.config import Settings
from govcon.decision.provider import DecisionProviderUnavailable
from govcon.decision.providers.jev import JevDecisionProvider
from govcon.decision.providers.llm_fallback import LLMDecisionProvider
from govcon.enrich.attachments import process_local_file
from govcon.enrich.file_class import public_blockers
from govcon.models import AuditEvent, Opportunity, StoredFile
from govcon.prompting.registry import PromptSetupError, require_prompt_registry
from govcon.security.classification import DataClassification

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "solicitation_fixture.pdf"


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as db:
        yield db
        db.rollback()


def _opp(session: Session) -> Opportunity:
    row = Opportunity(source="sam", source_id=f"id-{uuid4().hex}", title="Identity", status="open", raw={}, links={})
    session.add(row)
    session.flush()
    return row


def test_a_fake_pdf_stays_unknown_and_a_different_file_with_the_same_name_does_not(session, tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    opp = _opp(session)
    fake = tmp_path / "SPE4A526T443K.pdf"
    fake.write_bytes(b"not-a-pdf" + b"x" * 24)
    real = tmp_path / "other" / "SPE4A526T443K.pdf"
    real.parent.mkdir()
    real.write_bytes(FIXTURE_PDF.read_bytes())
    assert fake.read_bytes() != real.read_bytes()

    fake_row = process_local_file(
        session, opp, fake, classification=DataClassification.PUBLIC,
        source_origin="named like the notice", settings=settings,
    )
    real_row = process_local_file(
        session, opp, real, classification=DataClassification.PUBLIC,
        source_origin="copied from the downloaded notice", settings=settings,
    )
    assert fake_row.id != real_row.id
    assert fake_row.sha256 != real_row.sha256
    assert fake_row.filename == real_row.filename == "SPE4A526T443K.pdf"
    assert fake_row.classification == "UNKNOWN"
    assert real_row.classification == "PUBLIC"
    assert Path(real_row.local_path) != real
    assert Path(fake_row.local_path).read_bytes() == fake.read_bytes()
    assert public_blockers(fake_row, fake.read_bytes())
    event = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action_type == "file_classification_changed",
            AuditEvent.entity_id == real_row.id,
        )
    )
    assert event is not None
    assert event.old_value["classification"] == "UNKNOWN"
    assert event.new_value["classification"] == "PUBLIC"
    assert event.new_value["sha256"] == real_row.sha256
    assert "nitrile" not in str(event.new_value).lower()
    assert "extracted_text" not in event.new_value


def test_filename_alone_is_not_a_reason_to_mark_public() -> None:
    row = StoredFile(
        filename="SPE4A526T443K.pdf",
        classification="UNKNOWN",
        source_origin="SPE4A526T443K.pdf",
        sha256="abc",
        extraction_status="success",
        extracted_text="This filename is not the document.",
    )
    reasons = public_blockers(row, b"%PDF-1.4 real-enough")
    assert any("filename" in reason for reason in reasons)


def test_a_sparse_part_stays_incomplete_after_the_summary_is_merged() -> None:
    from types import SimpleNamespace

    from govcon.ai.structured import ExecutedCall, PreparedCall
    from govcon.enrich.summarize import _merged_analysis

    prepared = PreparedCall(
        prompt=SimpleNamespace(name="solicitation_analysis", version="v1", content_hash="abc"),
        schema_cls=SolicitationAnalysisV1,
        schema_version="v1",
        classification=DataClassification.PUBLIC,
        provider=None,
        provider_name="deepseek",
        model="deepseek-flash",
        system_prompt="s",
        user_prompt="u",
        generation_settings={},
        opportunity_id=1,
        analysis_type="solicitation_summary",
        variables={},
        context_manifest={},
    )
    executed = ExecutedCall(
        output=SolicitationAnalysisV1(summary="ok"),
        result=SimpleNamespace(provider="deepseek", model="deepseek-flash", usage=None, latency_ms=1),
        reservation=None,
        quality="incomplete",
        quality_reason="sparse",
    )
    analysis = _merged_analysis([(prepared, executed)], {"summary": "ok"}, {})
    assert analysis.generation_settings["quality"] == "incomplete"
    assert analysis.generation_settings["quality_reason"] == "sparse"


def test_sparse_schema_valid_output_fails_the_quality_check() -> None:
    empty = SolicitationAnalysisV1.model_construct()
    sparse = SolicitationAnalysisV1(summary="ok")
    placeholders = SolicitationAnalysisV1(summary="not available")
    for output in (empty, sparse, placeholders):
        quality, reason = assess_output_quality(output)
        assert quality == "incomplete"
        assert reason
        assert quality != "accepted"


def test_blocked_classes_never_open_an_outbound_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("outbound HTTP client was opened")

    monkeypatch.setattr("govcon.http.build_client", refuse)
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        jev_api_key="test-key",
        ai_external_allowed_for_proprietary=False,
        ai_external_allowed_for_fci=False,
        ai_external_allowed_for_cui=False,
    )
    deepseek = DeepSeekProvider("test-key", settings=settings)
    jev = JevDecisionProvider(api_key="test-key", settings=settings)
    fallback = LLMDecisionProvider(settings=settings)
    for classification in (
        DataClassification.UNKNOWN,
        DataClassification.PROPRIETARY,
        DataClassification.FCI,
        DataClassification.CUI,
    ):
        assert external_call_allowed(classification, settings) is False
        for _attempt in range(2):
            with pytest.raises(AIGatewayBlocked):
                deepseek.complete(
                    system_prompt="system", user_prompt="user", classification=classification, purpose="test",
                )
            with pytest.raises(DecisionProviderUnavailable):
                jev.decide(
                    bundle_name="opportunity_triage",
                    bundle_version="v1",
                    state={"data_classification": classification.value},
                )
            with pytest.raises(DecisionProviderUnavailable):
                fallback.decide(
                    bundle_name="opportunity_triage",
                    bundle_version="v1",
                    state={"data_classification": classification.value},
                )


def test_missing_prompts_name_the_sync_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("govcon.prompting.registry.active_version", lambda session, name: None)
    monkeypatch.setattr("govcon.prompting.registry.sync_prompts", lambda *args, **kwargs: None)
    with pytest.raises(PromptSetupError, match="govcon prompts sync") as caught:
        require_prompt_registry(Session(), Settings(_env_file=None))
    assert "solicitation_analysis" in caught.value.missing
