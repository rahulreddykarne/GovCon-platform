"""Semantic search and recommendation engine (Phase 13).

Uses pgvector cosine distance (``<=>`` operator) to find nearest neighbours.
Hard eligibility filters are applied after vector search so semantically
similar but ineligible opportunities are always flagged rather than silently
excluded or auto-pursued.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.matching.eligibility import ELIGIBLE_STATUSES, pursuit_eligibility
from govcon.models import Match, Opportunity, Watchlist

if TYPE_CHECKING:
    from govcon.enrich.embeddings import EmbeddingProvider

log = logging.getLogger(__name__)

# ── recommendation category labels ───────────────────────────────────────────

CATEGORY_RULE_MATCH = "Rule match"
CATEGORY_SEMANTIC_MATCH = "Semantic match"
CATEGORY_SIMILAR_WON = "Similar to won bids"
CATEGORY_SIMILAR_PURSUED = "Similar to pursued bids"
CATEGORY_RECOMPETE_RADAR = "Recompete radar"

# Ingest normalizes actionable listings to the canonical open status.
_ELIGIBLE_STATUSES = ELIGIBLE_STATUSES

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


def is_eligible_for_pursuit(opp: Opportunity) -> bool:
    """Return True when an opportunity is open and could be legitimately pursued."""
    return pursuit_eligibility(opp)[0] == "eligible"


def _compact_opp(opp: Opportunity, *, distance: float | None = None) -> dict[str, Any]:
    eligibility, reason = pursuit_eligibility(opp)
    result: dict[str, Any] = {
        "id": opp.id,
        "title": opp.title,
        "source": opp.source,
        "source_id": opp.source_id,
        "psc_code": opp.psc_code,
        "naics_code": opp.naics_code,
        "nsn": opp.nsn,
        "agency_path": opp.agency_path,
        "status": opp.status,
        "response_deadline": opp.response_deadline.isoformat() if opp.response_deadline else None,
        "eligible_for_pursuit": eligibility == "eligible",
        "eligibility_status": eligibility,
    }
    if distance is not None:
        result["cosine_distance"] = round(distance, 4)
    if reason:
        result["ineligible_reason"] = reason
    return result


# ── core vector search ────────────────────────────────────────────────────────


def _vector_search(
    session: Session,
    query_embedding: list[float],
    *,
    model_id: str | None,
    exclude_id: int | None = None,
    exclude_watchlist_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[tuple[Opportunity, float]]:
    """Return (opportunity, cosine_distance) pairs ordered by closeness.

    Hard eligibility is NOT applied here; callers decide what to do with
    ineligible results (flag them, not auto-pursue).
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    if not model_id or len(query_embedding) != 384 or not all(math.isfinite(v) for v in query_embedding):
        return []

    # pgvector <=> operator returns cosine distance (0 = identical, 2 = opposite).
    # We embed the vector as a literal cast so psycopg does not need special handling.
    distance = Opportunity.embedding.cosine_distance(list(query_embedding))
    # Match opportunity_text: omit empty fields, join with spaces, strip edges.
    content = func.regexp_replace(func.concat_ws(" ", *[
        func.nullif(column, "") for column in (Opportunity.title, Opportunity.description,
        Opportunity.psc_code, Opportunity.naics_code, Opportunity.nsn, Opportunity.agency_path)
    ]), r"^\s+|\s+$", "", "g")
    source_hash = func.encode(func.sha256(func.convert_to(content, "UTF8")), "hex")
    stmt = (
        select(Opportunity, distance.label("cosine_dist"))
        .where(Opportunity.embedding.is_not(None))
        .where(Opportunity.embedding_model == model_id, Opportunity.embedding_dimension == 384,
               Opportunity.embedding_source_hash == source_hash)
        .order_by(distance, Opportunity.id)
        .limit(limit)
    )
    if exclude_id is not None:
        stmt = stmt.where(Opportunity.id != exclude_id)
    if exclude_watchlist_id is not None:
        # Only live, non-dismissed rule matches are already surfaced. Historical
        # matches may become useful again and must not hide recommendations.
        existing_match = select(Match.id).where(
            Match.watchlist_id == exclude_watchlist_id,
            Match.opportunity_id == Opportunity.id,
            Match.active.is_(True),
            Match.status.in_(["new", "seen", "reviewing", "pursuing"]),
        ).exists()
        stmt = stmt.where(~existing_match)

    rows = session.execute(stmt).all()
    return [(row[0], float(row[1])) for row in rows[:limit]]


# ── public search functions ───────────────────────────────────────────────────


def similar_opportunities(
    session: Session,
    opportunity_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Find opportunities semantically similar to a given opportunity.

    Falls back to heuristic (NSN/PSC/agency) when the target has no embedding.
    Ineligible results are flagged but still returned (spec AC: flagged, not auto-pursued).
    """
    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": f"Opportunity {opportunity_id} not found"}}

    limit = max(1, min(limit, _MAX_LIMIT))

    from govcon.enrich.embeddings import _refresh, _source_hash, opportunity_text

    if provider is not None:
        _refresh(opp, provider)
        session.flush()
    if (opp.embedding is not None and opp.embedding_model and opp.embedding_dimension == 384
            and opp.embedding_source_hash == _source_hash(opportunity_text(opp))):
        results = _vector_search(session, opp.embedding, model_id=opp.embedding_model,
                                 exclude_id=opportunity_id, limit=limit)
        matches = [
            _compact_opp(o, distance=d)
            for o, d in results
        ]
        return {
            "opportunity_id": opportunity_id,
            "method": "vector",
            "matches": matches,
        }

    # Final heuristic (no provider, no embedding)
    from sqlalchemy import or_

    filters = []
    if opp.nsn:
        filters.append(Opportunity.nsn == opp.nsn)
    if opp.psc_code:
        filters.append(Opportunity.psc_code == opp.psc_code)
    if opp.agency_path:
        filters.append(Opportunity.agency_path == opp.agency_path)
    if not filters:
        return {
            "opportunity_id": opportunity_id,
            "method": "heuristic",
            "note": "No embedding and no NSN/PSC/agency anchors; run 'govcon embed run' first.",
            "matches": [],
        }
    rows = session.scalars(
        select(Opportunity)
        .where(Opportunity.id != opportunity_id)
        .where(or_(*filters))
        .order_by(Opportunity.response_deadline.desc().nullslast(), Opportunity.id.desc())
        .limit(limit)
    ).all()
    return {
        "opportunity_id": opportunity_id,
        "method": "heuristic",
        "note": "Embedding missing; run 'govcon embed run' for vector search.",
        "matches": [_compact_opp(o) for o in rows],
    }


def semantic_recommendations_for_watchlist(
    session: Session,
    watchlist_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return semantic recommendations for a watchlist using its profile embedding.

    Opportunities already matched by rule (Phase 2 engine) are excluded so this
    surfaces only keyword-miss candidates (spec task 4).
    """
    wl = session.get(Watchlist, watchlist_id)
    if wl is None:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": f"Watchlist {watchlist_id} not found"}}

    if wl.embedding is None or not wl.embedding_model or wl.embedding_dimension != 384:
        return {
            "watchlist_id": watchlist_id,
            "category": CATEGORY_SEMANTIC_MATCH,
            "note": "Watchlist has no profile embedding; run 'govcon embed watchlists' first.",
            "matches": [],
        }

    limit = max(1, min(limit, _MAX_LIMIT))
    results = _vector_search(session, wl.embedding, model_id=wl.embedding_model,
                             limit=limit, exclude_watchlist_id=watchlist_id)
    matches = [_compact_opp(opp, distance=dist) for opp, dist in results]

    return {
        "watchlist_id": watchlist_id,
        "category": CATEGORY_SEMANTIC_MATCH,
        "note": "Opportunities with semantic similarity to this watchlist that keyword rules missed.",
        "matches": matches,
    }


def win_profile_recommendations(
    session: Session,
    provider: EmbeddingProvider,
    *,
    limit: int = _DEFAULT_LIMIT,
    min_wins: int = 3,
) -> dict[str, Any]:
    """Return opportunities similar to won bids.

    Returns an empty list with an explanatory note when fewer than ``min_wins``
    genuine wins exist (spec task 5).
    """
    from govcon.enrich.embeddings import _model_id, compute_win_profile

    win_embedding = compute_win_profile(session, provider, min_wins=min_wins)
    if win_embedding is None:
        from govcon.learning.analytics import win_count
        actual = win_count(session)
        return {
            "category": CATEGORY_SIMILAR_WON,
            "note": f"Win-profile requires at least {min_wins} recorded wins (have {actual}).",
            "matches": [],
        }

    results = _vector_search(session, win_embedding, model_id=_model_id(provider), limit=limit)
    return {
        "category": CATEGORY_SIMILAR_WON,
        "matches": [_compact_opp(o, distance=d) for o, d in results],
    }


def pursued_profile_recommendations(
    session: Session,
    provider: EmbeddingProvider,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return opportunities similar to actively pursued bids."""
    from govcon.enrich.embeddings import _model_id, compute_pursued_profile

    pursued_embedding = compute_pursued_profile(session, provider)
    if pursued_embedding is None:
        return {
            "category": CATEGORY_SIMILAR_PURSUED,
            "note": "No actively pursued bids found; pursue at least one opportunity first.",
            "matches": [],
        }

    results = _vector_search(session, pursued_embedding, model_id=_model_id(provider), limit=limit)
    return {
        "category": CATEGORY_SIMILAR_PURSUED,
        "matches": [_compact_opp(o, distance=d) for o, d in results],
    }


def recompete_radar(
    session: Session,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return open opportunities that look like recompetes of past awards.

    Uses the ``award_recompete_candidates`` set (Phase 5) when present.
    Falls back to solicitation-number heuristic.
    """
    # Recompete candidates are opportunities that share a prior award's PSC/NAICS/agency
    # and whose solicitation number looks like an amendment/follow-on.
    # Phase 5 stored recompete candidate flags in awards.recompete_candidates.
    from govcon.models import Award

    limit = max(1, min(limit, _MAX_LIMIT))
    award_opps_stmt = (
        select(Opportunity)
        .join(
            Award,
            (Award.psc_code == Opportunity.psc_code) | (Award.awarding_agency == Opportunity.agency_path),
        )
        .where(Opportunity.status.in_(_ELIGIBLE_STATUSES))
        .distinct()
        .limit(limit)
    )
    rows = session.scalars(award_opps_stmt).all()

    return {
        "category": CATEGORY_RECOMPETE_RADAR,
        "note": "Open opportunities sharing PSC or agency with a past award.",
        "matches": [_compact_opp(o) for o in rows],
    }


def all_recommendations(
    session: Session,
    provider: EmbeddingProvider,
    *,
    watchlist_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    min_wins: int = 3,
) -> dict[str, Any]:
    """Return recommendations in all five categories.

    Semantic match category requires a watchlist_id to use that watchlist's
    profile embedding. When omitted, semantic match is skipped.
    """
    categories: dict[str, Any] = {}

    if watchlist_id is not None:
        categories[CATEGORY_SEMANTIC_MATCH] = semantic_recommendations_for_watchlist(
            session, watchlist_id, limit=limit
        )

    categories[CATEGORY_SIMILAR_WON] = win_profile_recommendations(
        session, provider, limit=limit, min_wins=min_wins
    )
    categories[CATEGORY_SIMILAR_PURSUED] = pursued_profile_recommendations(
        session, provider, limit=limit
    )
    categories[CATEGORY_RECOMPETE_RADAR] = recompete_radar(session, limit=limit)

    return {
        "categories": categories,
        "note": (
            "Rule match results are in govcon match run output. "
            "Semantic match, Similar to won/pursued, and Recompete radar are shown here."
        ),
    }
