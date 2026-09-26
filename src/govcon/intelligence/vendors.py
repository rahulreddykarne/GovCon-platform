"""Vendor profiles and award statistics (Phase 6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.ingest.sam_entities import SamEntityError, ensure_vendor, normalize_uei
from govcon.models import Award, Vendor


@dataclass(frozen=True)
class RankedValue:
    label: str
    count: int
    total_obligation: Decimal | None


@dataclass(frozen=True)
class VendorAwardStats:
    award_count: int
    total_obligation: Decimal | None
    top_agencies: tuple[RankedValue, ...]
    top_pscs: tuple[RankedValue, ...]


@dataclass(frozen=True)
class VendorProfile:
    uei: str
    cage_code: str | None
    legal_name: str | None
    dba_name: str | None
    registration_status: str | None
    business_types: dict | None
    naics_codes: list | None
    psc_codes: list | None
    physical_address: dict | None
    points_of_contact: dict | list | None
    fetched_at: datetime | None
    from_cache: bool
    award_stats: VendorAwardStats


def _ranked(
    session: Session,
    *,
    group_column,
    recipient_uei: str,
    limit: int = 5,
) -> tuple[RankedValue, ...]:
    statement = (
        select(
            group_column,
            func.count().label("award_count"),
            func.sum(Award.total_obligation).label("total_obligation"),
        )
        .where(Award.recipient_uei == recipient_uei)
        .where(group_column.is_not(None))
        .where(func.trim(group_column) != "")
        .group_by(group_column)
        .order_by(
            func.count().desc(),
            func.sum(Award.total_obligation).desc().nulls_last(),
        )
        .limit(limit)
    )
    ranked: list[RankedValue] = []
    for label, award_count, total_obligation in session.execute(statement):
        ranked.append(
            RankedValue(
                label=str(label),
                count=int(award_count),
                total_obligation=total_obligation,
            )
        )
    return tuple(ranked)


def award_stats_for_uei(session: Session, uei: str, *, limit: int = 5) -> VendorAwardStats:
    normalized = normalize_uei(uei)
    totals = session.execute(
        select(func.count(), func.sum(Award.total_obligation)).where(Award.recipient_uei == normalized)
    ).one()
    award_count = int(totals[0] or 0)
    total_obligation = totals[1]
    return VendorAwardStats(
        award_count=award_count,
        total_obligation=total_obligation,
        top_agencies=_ranked(session, group_column=Award.awarding_agency, recipient_uei=normalized, limit=limit),
        top_pscs=_ranked(session, group_column=Award.psc_code, recipient_uei=normalized, limit=limit),
    )


def _profile_from_vendor(vendor: Vendor, *, from_cache: bool, stats: VendorAwardStats) -> VendorProfile:
    return VendorProfile(
        uei=vendor.uei,
        cage_code=vendor.cage_code,
        legal_name=vendor.legal_name,
        dba_name=vendor.dba_name,
        registration_status=vendor.registration_status,
        business_types=vendor.business_types,
        naics_codes=vendor.naics_codes if isinstance(vendor.naics_codes, list) else None,
        psc_codes=vendor.psc_codes if isinstance(vendor.psc_codes, list) else None,
        physical_address=vendor.physical_address,
        points_of_contact=vendor.points_of_contact,
        fetched_at=vendor.fetched_at,
        from_cache=from_cache,
        award_stats=stats,
    )


def vendor_profile(
    session: Session,
    uei: str,
    *,
    refresh: bool = False,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> VendorProfile:
    """Return SAM registration data plus computed award statistics for one UEI."""
    settings = settings or get_settings()
    vendor, fetched_live = ensure_vendor(
        session,
        uei,
        refresh=refresh,
        client=client,
        settings=settings,
        now=now,
    )
    stats = award_stats_for_uei(session, vendor.uei)
    return _profile_from_vendor(vendor, from_cache=not fetched_live, stats=stats)


def vendor_profile_from_row(session: Session, vendor: Vendor) -> VendorProfile:
    """Build a profile from an existing vendor row without calling SAM."""
    stats = award_stats_for_uei(session, vendor.uei)
    return _profile_from_vendor(vendor, from_cache=True, stats=stats)


__all__ = [
    "RankedValue",
    "SamEntityError",
    "VendorAwardStats",
    "VendorProfile",
    "award_stats_for_uei",
    "vendor_profile",
    "vendor_profile_from_row",
]
