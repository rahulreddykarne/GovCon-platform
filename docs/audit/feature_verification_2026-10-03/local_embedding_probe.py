"""Offline audit probe using the cached real embedding model and disposable DB."""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from sqlalchemy.orm import Session

from govcon.enrich.embeddings import SentenceTransformerProvider, run_embedding_job
from govcon.matching.semantic import similar_opportunities
from govcon.models import Opportunity


def test_cached_model_and_pgvector_search(upgraded_engine, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    provider = SentenceTransformerProvider("all-MiniLM-L6-v2")
    cache = Path.home() / ".cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2"
    cached_revision = (cache / "refs/main").read_text(encoding="utf-8").strip()
    with Session(upgraded_engine) as db:
        rows = [
            Opportunity(source="sam", source_id="audit-real-embedding-gloves",
                        title="Disposable nitrile examination gloves", status="open",
                        description="Medical protective gloves for hospital examinations",
                        response_deadline=datetime.now(UTC) + timedelta(days=14), raw={}),
            Opportunity(source="sam", source_id="audit-real-embedding-medical",
                        title="Nitrile medical exam gloves", status="open",
                        description="Disposable hospital hand protection for clinicians",
                        response_deadline=datetime.now(UTC) + timedelta(days=14), raw={}),
            Opportunity(source="sam", source_id="audit-real-embedding-road",
                        title="Asphalt highway reconstruction", status="open",
                        description="Road paving and bridge maintenance construction",
                        response_deadline=datetime.now(UTC) + timedelta(days=14), raw={}),
        ]
        db.add_all(rows)
        db.flush()
        counts = run_embedding_job(db, provider)
        repeated = run_embedding_job(db, provider)
        result = similar_opportunities(db, rows[0].id)
        assert counts == {"embedded": 3, "skipped": 0}
        assert repeated == {"embedded": 0, "skipped": 3}
        assert all(row.embedding_dimension == 384 for row in rows)
        assert result["method"] == "vector"
        assert result["matches"][0]["id"] == rows[1].id
        assert result["matches"][0]["cosine_distance"] < result["matches"][1]["cosine_distance"]
        # Record the production provider's provenance failure, not just fake-provider behavior.
        assert len(cached_revision) == 40
        assert provider.model_version == "all-MiniLM-L6-v2@unversioned"
        evidence = {"offline": True, "model_version": provider.model_version,
                    "cached_revision": cached_revision,
                    "counts": counts, "repeat_counts": repeated,
                    "dimension": 384, "similarity_result": result}
        Path(__file__).with_name("local-embedding-results.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
        db.rollback()
