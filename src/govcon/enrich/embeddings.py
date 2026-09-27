"""Embedding generation for opportunities and watchlist profiles (Phase 13).

Uses sentence-transformers locally (``all-MiniLM-L6-v2`` by default, 384 dims).
The model is lazy-loaded on first call and cached for the process lifetime.

In tests, pass a ``provider`` object with an ``embed(text: str) -> list[float]``
method to avoid loading the real model.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Opportunity, OutcomeFeedback, Watchlist

log = logging.getLogger(__name__)

_EMBEDDING_DIM = 384


# ── provider protocol ─────────────────────────────────────────────────────────


class EmbeddingProvider(Protocol):
    """Minimal interface for an embedding back-end."""

    def embed(self, text: str) -> list[float]:
        ...


class SentenceTransformerProvider:
    """Real provider: sentence-transformers loaded from disk."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("Loading embedding model %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._load()
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()


def get_default_provider(model_name: str | None = None) -> SentenceTransformerProvider:
    """Return a lazily-loaded SentenceTransformerProvider."""
    from govcon.config import get_settings

    name = model_name or get_settings().embedding_model
    return SentenceTransformerProvider(name)


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
    result = [0.0] * dim
    for vec in vectors:
        for i, v in enumerate(vec):
            result[i] += v
    n = len(vectors)
    return [x / n for x in result]


# ── job functions ─────────────────────────────────────────────────────────────


def embed_opportunity(
    opp: Opportunity,
    provider: EmbeddingProvider,
) -> list[float]:
    """Return an embedding for a single opportunity."""
    text = opportunity_text(opp)
    if not text:
        return [0.0] * _EMBEDDING_DIM
    return provider.embed(text)


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
    if only_missing:
        stmt = stmt.where(Opportunity.embedding.is_(None))
    opps = session.scalars(stmt).all()

    embedded = 0
    skipped = 0
    for opp in opps:
        text = opportunity_text(opp)
        if not text:
            skipped += 1
            continue
        opp.embedding = provider.embed(text)
        embedded += 1
        if embedded % batch_size == 0:
            session.flush()

    if embedded:
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
            vectors.append(provider.embed(profile_text))
        wl.embedding = vectors[0] if len(vectors) == 1 else _mean_pool(vectors) or [0.0] * _EMBEDDING_DIM
        wl.embedding_updated_at = datetime.now(UTC)
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
    won_opp_ids = [
        row.opportunity_id
        for row in session.scalars(
            select(OutcomeFeedback).where(OutcomeFeedback.outcome == "won")
        ).all()
    ]
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
        if opp.embedding is not None:
            vectors.append(opp.embedding)
        else:
            text = opportunity_text(opp)
            if text:
                vectors.append(provider.embed(text))

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
        if opp.embedding is not None:
            vectors.append(opp.embedding)
        else:
            text = opportunity_text(opp)
            if text:
                vectors.append(provider.embed(text))

    return _mean_pool(vectors)
