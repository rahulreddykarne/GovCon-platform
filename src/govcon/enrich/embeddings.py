"""Embedding generation for opportunities and watchlist profiles (Phase 13).

Uses sentence-transformers locally (``all-MiniLM-L6-v2`` by default, 384 dims).
The model is lazy-loaded on first call and cached for the process lifetime.

In tests, pass a ``provider`` object with an ``embed(text: str) -> list[float]``
method to avoid loading the real model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Match, Opportunity, OutcomeFeedback, Watchlist

log = logging.getLogger(__name__)

_EMBEDDING_DIM = 384


# ── provider protocol ─────────────────────────────────────────────────────────


class EmbeddingProvider(Protocol):
    """Minimal interface for an embedding back-end."""

    def embed(self, text: str) -> list[float]:
        ...


class SentenceTransformerProvider:
    """Real provider: sentence-transformers loaded from disk."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", *, revision: str | None = None) -> None:
        self._model_name = model_name
        self._revision = revision
        self._model: Any = None
        self._model_version: str | None = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("Loading embedding model %s", self._model_name)
            self._model = SentenceTransformer(self._model_name, revision=self._revision)
        return self._model

    @property
    def model_version(self) -> str:
        """Include the resolved model commit, rather than only its mutable alias."""
        if self._model_version is None:
            model = self._load()
            config = getattr(getattr(model[0], "auto_model", None), "config", None)
            revision = getattr(config, "_commit_hash", None) or self._revision
            if revision is None:
                from pathlib import Path
                root = Path(self._model_name)
                if not root.is_dir():
                    from huggingface_hub import try_to_load_from_cache

                    repo_id = getattr(config, "_name_or_path", None) or self._model_name
                    if "/" not in repo_id:
                        repo_id = f"sentence-transformers/{repo_id}"
                    cached = try_to_load_from_cache(repo_id, "config.json", revision=self._revision)
                    if isinstance(cached, str) and Path(cached).parent.parent.name == "snapshots":
                        revision = Path(cached).parent.name
                files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in {".json", ".bin", ".safetensors"}) if root.is_dir() else []
                digest = hashlib.sha256()
                for path in files:
                    digest.update(str(path.relative_to(root)).encode())
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                revision = revision or (digest.hexdigest() if files else "unversioned")
            self._model_version = f"{self._model_name}@{revision}"
        return self._model_version

    def embed(self, text: str) -> list[float]:
        model = self._load()
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()


def get_default_provider(model_name: str | None = None) -> SentenceTransformerProvider:
    """Return a lazily-loaded SentenceTransformerProvider."""
    from govcon.config import get_settings

    settings = get_settings()
    name = model_name or settings.embedding_model
    return SentenceTransformerProvider(name, revision=settings.embedding_model_revision)


# ── text builders ─────────────────────────────────────────────────────────────


def opportunity_text(opp: Opportunity) -> str:
    """Concatenate the fields most useful for semantic matching."""
    parts = [
        opp.title or "",
        opp.description or "",
        opp.psc_code or "",
        opp.naics_code or "",
        opp.nsn or "",
        opp.agency_path or "",
    ]
    return " ".join(p for p in parts if p).strip()


def watchlist_profile_text(wl: Watchlist) -> str:
    """Build a representative text blob from a watchlist's matching criteria."""
    parts: list[str] = []
    if wl.name:
        parts.append(wl.name)
    for field in (wl.keywords, wl.psc_codes, wl.naics_codes, wl.nsn_list):
        if field:
            parts.extend(field)
    if wl.notes:
        parts.append(wl.notes)
    return " ".join(parts).strip()


# ── mean-pooling helper ───────────────────────────────────────────────────────


def _mean_pool(vectors: list[list[float]]) -> list[float] | None:
    """Return component-wise mean of a list of equal-length vectors."""
    if not vectors:
        return None
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        raise ValueError("embedding vectors must have the same dimension")
    result = [0.0] * dim
    for vec in vectors:
        for i, v in enumerate(vec):
            result[i] += v
    n = len(vectors)
    return [x / n for x in result]


def _model_id(provider: EmbeddingProvider) -> str:
    return str(getattr(provider, "model_version", None) or getattr(provider, "model_name", None) or getattr(provider, "_model_name", None) or f"{type(provider).__module__}.{type(provider).__qualname__}")


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vector(provider: EmbeddingProvider, text: str) -> list[float]:
    vector = list(provider.embed(text))
    if len(vector) != _EMBEDDING_DIM or not all(math.isfinite(v) for v in vector):
        raise ValueError(f"embedding model must return {_EMBEDDING_DIM} finite values; change the vector schema before using another dimension")
    return vector


def _fresh(opp: Opportunity, provider: EmbeddingProvider, text: str) -> bool:
    return (opp.embedding is not None and opp.embedding_model == _model_id(provider)
            and opp.embedding_dimension == getattr(provider, "dimension", getattr(provider, "DIM", _EMBEDDING_DIM))
            and opp.embedding_dimension == _EMBEDDING_DIM and len(opp.embedding) == _EMBEDDING_DIM
            and opp.embedding_source_hash == _source_hash(text))


def _refresh(opp: Opportunity, provider: EmbeddingProvider) -> list[float] | None:
    text = opportunity_text(opp)
    if not text:
        opp.embedding = None
        opp.embedding_model = None
        opp.embedding_dimension = None
        opp.embedding_source_hash = None
        return None
    if not _fresh(opp, provider, text):
        opp.embedding = _vector(provider, text)
        opp.embedding_model = _model_id(provider)
        opp.embedding_dimension = _EMBEDDING_DIM
        opp.embedding_source_hash = _source_hash(text)
    assert opp.embedding is not None  # fresh or just generated above
    return list(opp.embedding)


# ── job functions ─────────────────────────────────────────────────────────────


def embed_opportunity(
    opp: Opportunity,
    provider: EmbeddingProvider,
) -> list[float]:
    """Return an embedding for a single opportunity."""
    text = opportunity_text(opp)
    if not text:
        return [0.0] * _EMBEDDING_DIM
    return _vector(provider, text)


def run_embedding_job(
    session: Session,
    provider: EmbeddingProvider,
    *,
    batch_size: int = 200,
    only_missing: bool = True,
) -> dict[str, int]:
    """Embed all (or all un-embedded) opportunities and persist the vectors.

    Returns ``{"embedded": N, "skipped": M}`` counts.
    """
    stmt = select(Opportunity)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    opps = session.scalars(stmt).all()

    embedded = 0
    skipped = 0
    for opp in opps:
        text = opportunity_text(opp)
        if not text:
            _refresh(opp, provider)
            skipped += 1
            continue
        if only_missing and _fresh(opp, provider, text):
            skipped += 1
            continue
        if not only_missing:
            opp.embedding_source_hash = None
        _refresh(opp, provider)
        embedded += 1
        if embedded % batch_size == 0:
            session.flush()

    session.flush()
    log.info("Embedding job complete: embedded=%d skipped=%d", embedded, skipped)
    return {"embedded": embedded, "skipped": skipped}


def build_watchlist_profiles(
    session: Session,
    provider: EmbeddingProvider,
    *,
    watchlist_id: int | None = None,
) -> dict[str, int]:
    """Compute and persist a profile embedding for each enabled watchlist.

    The profile is the mean of:
      1. The embedding of the watchlist's descriptive text (name + keywords …)
      2. The mean embedding of all opportunities that have been matched to this
         watchlist and have an embedding.

    Returns ``{"updated": N}`` count.
    """
    stmt = select(Watchlist).where(Watchlist.enabled.is_(True))
    if watchlist_id is not None:
        stmt = stmt.where(Watchlist.id == watchlist_id)
    watchlists = session.scalars(stmt).all()

    updated = 0
    for wl in watchlists:
        profile_text = watchlist_profile_text(wl)
        vectors: list[list[float]] = []
        if profile_text:
            vectors.append(_vector(provider, profile_text))
        matched = session.scalars(select(Opportunity).join(Match, Match.opportunity_id == Opportunity.id).where(Match.watchlist_id == wl.id, Match.active.is_(True)).order_by(Opportunity.id)).all()
        matched_vectors = [v for opp in matched if (v := _refresh(opp, provider)) is not None]
        if matched_vectors:
            pooled = _mean_pool(matched_vectors)
            assert pooled is not None  # the input list is nonempty
            vectors.append(pooled)
        wl.embedding = vectors[0] if len(vectors) == 1 else _mean_pool(vectors) or [0.0] * _EMBEDDING_DIM
        wl.embedding_updated_at = datetime.now(UTC)
        wl.embedding_model = _model_id(provider)
        wl.embedding_dimension = _EMBEDDING_DIM
        wl.embedding_source_hash = _source_hash(json.dumps([profile_text, [(o.id, o.embedding_source_hash) for o in matched]], sort_keys=True))
        updated += 1

    if updated:
        session.flush()
    log.info("Watchlist profile build complete: updated=%d", updated)
    return {"updated": updated}


def compute_win_profile(
    session: Session,
    provider: EmbeddingProvider,
    *,
    min_wins: int = 3,
) -> list[float] | None:
    """Return a win-profile embedding (mean of won opportunity embeddings).

    Returns ``None`` when fewer than ``min_wins`` genuine wins exist.
    """
    from govcon.learning.analytics import current_outcomes
    won_opp_ids = list(session.scalars(select(OutcomeFeedback.opportunity_id).where(current_outcomes(), OutcomeFeedback.outcome == "won").distinct()))
    if len(won_opp_ids) < min_wins:
        log.info(
            "Win profile skipped: only %d won bids (need %d)", len(won_opp_ids), min_wins
        )
        return None

    opps = session.scalars(
        select(Opportunity).where(Opportunity.id.in_(won_opp_ids))
    ).all()

    vectors: list[list[float]] = []
    for opp in opps:
        vector = _refresh(opp, provider)
        if vector is not None:
            vectors.append(vector)

    return _mean_pool(vectors)


def compute_pursued_profile(
    session: Session,
    provider: EmbeddingProvider,
) -> list[float] | None:
    """Return a profile embedding from actively pursued opportunities."""
    from govcon.models import Pursuit

    # `stage` is the Pursuit field (not status). Active pursuit stages below `submitted`.
    pursuing_opp_ids = [
        row.opportunity_id
        for row in session.scalars(
            select(Pursuit).where(
                Pursuit.stage.in_(["evaluating", "bid_approved", "sourcing", "drafting", "review", "ready_to_submit"])
            )
        ).all()
    ]
    if not pursuing_opp_ids:
        return None

    opps = session.scalars(
        select(Opportunity).where(Opportunity.id.in_(pursuing_opp_ids))
    ).all()

    vectors: list[list[float]] = []
    for opp in opps:
        vector = _refresh(opp, provider)
        if vector is not None:
            vectors.append(vector)

    return _mean_pool(vectors)
