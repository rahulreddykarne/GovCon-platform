"""Real cached model, no network, and disposable PostgreSQL feature recheck."""
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.enrich.embeddings import (SentenceTransformerProvider, run_embedding_job,
                                      build_watchlist_profiles, compute_pursued_profile, compute_win_profile)
from govcon.matching.semantic import all_recommendations, similar_opportunities
from govcon.matching.recommendations import refresh_recommendations
from govcon.models import Match, Opportunity, Pursuit, Recommendation, Watchlist


def test_cached_real_model_revision_vectors_profiles_and_recommendations(upgraded_engine, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    provider = SentenceTransformerProvider("all-MiniLM-L6-v2")
    cache = Path.home() / ".cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2"
    revision = (cache / "refs/main").read_text().strip()
    with Session(upgraded_engine) as db:
        descriptions = [("Disposable nitrile examination gloves", "Medical protective gloves for hospital examinations"),
                        ("Nitrile medical exam gloves", "Disposable hospital hand protection for clinicians"),
                        ("Asphalt highway reconstruction", "Road paving and bridge maintenance construction")]
        rows = [Opportunity(source="sam", source_id=f"fix-real-model-{i}", title=title, description=text,
                            status="open", raw={}, response_deadline=datetime.now(UTC) + timedelta(days=14))
                for i, (title, text) in enumerate(descriptions)]
        db.add_all(rows)
        db.flush()
        counts = run_embedding_job(db, provider)
        repeated = run_embedding_job(db, provider)
        assert counts == {"embedded": 3, "skipped": 0}
        assert repeated == {"embedded": 0, "skipped": 3}
        assert provider.model_version == f"all-MiniLM-L6-v2@{revision}"
        assert len(revision) == 40
        assert all(row.embedding_dimension == 384 and all(math.isfinite(x) for x in row.embedding) for row in rows)
        rows[0].embedding_model = "all-MiniLM-L6-v2@unversioned"
        upgraded = run_embedding_job(db, provider)
        assert upgraded == {"embedded": 1, "skipped": 2}
        similar = similar_opportunities(db, rows[0].id)
        assert similar["method"] == "vector" and similar["matches"][0]["id"] == rows[1].id
        watchlist = Watchlist(name="Medical gloves", enabled=True, keywords=["nitrile", "medical", "gloves"])
        db.add(watchlist)
        db.flush()
        db.add(Match(opportunity_id=rows[0].id, watchlist_id=watchlist.id, active=True, score=1, matched_on={}))
        db.add(Pursuit(opportunity_id=rows[0].id, stage="evaluating"))
        db.flush()
        assert build_watchlist_profiles(db, provider, watchlist_id=watchlist.id) == {"updated": 1}
        assert watchlist.embedding_model == provider.model_version and watchlist.embedding_dimension == 384
        assert len(compute_pursued_profile(db, provider)) == 384
        assert compute_win_profile(db, provider) is None
        categories = all_recommendations(db, provider, watchlist_id=watchlist.id)
        refresh_recommendations(db, provider)
        saved = list(db.scalars(select(Recommendation).where(Recommendation.active.is_(True))))
        assert saved and all(row.embedding_model_version == provider.model_version for row in saved)
        ids = {row.id for row in saved}
        refresh_recommendations(db, provider)
        assert {row.id for row in db.scalars(select(Recommendation).where(Recommendation.active.is_(True)))} == ids
        evidence = {"offline": True, "model_version": provider.model_version, "cached_revision": revision,
                    "counts": counts, "repeat_counts": repeated, "dimension": 384,
                    "unversioned_upgrade_counts": upgraded, "persisted_recommendations": len(saved),
                    "similarity_result": similar, "watchlist_model": watchlist.embedding_model,
                    "recommendations": categories}
        Path(__file__).with_name("local-model-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        db.rollback()
