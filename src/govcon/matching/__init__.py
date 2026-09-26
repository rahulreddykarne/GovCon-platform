"""Deterministic watchlist matching."""

from govcon.matching.engine import MatchRunStats, rebuild_watchlist, run_matching
from govcon.matching.rules import EvaluationResult, evaluate_watchlist

__all__ = [
    "EvaluationResult",
    "MatchRunStats",
    "evaluate_watchlist",
    "rebuild_watchlist",
    "run_matching",
]
