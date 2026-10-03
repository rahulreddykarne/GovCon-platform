"""Deterministic compliance validators (§15.7).

Each validator returns ``ValidatorResult(status=pass|fail|unknown, reason,
evidence, validator_version)``. Missing inputs produce ``unknown`` — never
``fail`` and never ``pass``. LLM output cannot override a ``fail``; the status
gate in ``matrix.decide_status`` enforces that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from govcon.compliance.matrix import active_requirements, record_run
from govcon.compliance.records import Inventory, ValidatorResult
from govcon.models import Opportunity, Requirement

VALIDATOR_VERSION = "v1"
RUN_VERSION = "deterministic_validation.v1"

_TZ = {
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York", "Eastern": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago", "Central": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver", "Mountain": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles", "Pacific": "America/Los_Angeles",
    "UTC": "UTC", "GMT": "UTC", "Z": "UTC",
}
# SAM typeOfSetAside codes and DIBBS index codes (Y/H/R/L/A/E).
SET_ASIDE_STATUS = {
    "SBA": "small_business", "SBP": "small_business", "Y": "small_business",
    "H": "hubzone", "R": "sdvosb", "L": "wosb", "A": "8a", "E": "edwosb",
    "8A": "8a", "8AN": "8a", "HZC": "hubzone", "HZS": "hubzone",
    "SDVOSBC": "sdvosb", "SDVOSBS": "sdvosb", "WOSB": "wosb", "WOSBSS": "wosb",
    "EDWOSB": "edwosb", "EDWOSBSS": "edwosb", "VSA": "vosb", "VSS": "vosb",
}


def _result(name: str, status: str, reason: str, /, **evidence: Any) -> ValidatorResult:
    return ValidatorResult(name, status, reason, {k: _jsonable(v) for k, v in evidence.items()}, VALIDATOR_VERSION)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


# ── package model ──


@dataclass
class PackageFile:
    name: str
    size_bytes: int | None = None
    role: str = "other"
    form_id: str | None = None
    signed: bool | None = None
    page_count: int | None = None
    sha256: str | None = None
    local_path: str | None = None

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[-1].upper() if "." in self.name else ""


@dataclass
class SubmissionPackage:
    files: list[PackageFile] = field(default_factory=list)
    submission_method: str | None = None
    recipient_email: str | None = None
    portal: str | None = None
    planned_submission_at: datetime | None = None
    amendments_acknowledged: list[str] | None = None
    pricing_rows: list[dict[str, Any]] | None = None
    proposal_version_id: int | None = None
    representations_complete: bool | None = None
    certifications_complete: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmissionPackage:
        planned = data.get("planned_submission_at")
        return cls(
            files=[PackageFile(**f) for f in data.get("files", [])],
            submission_method=data.get("submission_method"),
            recipient_email=(data.get("recipient_email") or None),
            portal=data.get("portal"),
            planned_submission_at=datetime.fromisoformat(planned) if isinstance(planned, str) else planned,
            amendments_acknowledged=data.get("amendments_acknowledged"),
            pricing_rows=data.get("pricing_rows"),
            proposal_version_id=data.get("proposal_version_id"),
            representations_complete=data.get("representations_complete"),
            certifications_complete=data.get("certifications_complete"),
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "files": [dict(f.__dict__) for f in self.files],
            "submission_method": self.submission_method,
            "recipient_email": self.recipient_email,
            "portal": self.portal,
            "planned_submission_at": self.planned_submission_at.isoformat() if self.planned_submission_at else None,
            "amendments_acknowledged": self.amendments_acknowledged,
            "pricing_rows": self.pricing_rows,
            "proposal_version_id": self.proposal_version_id,
            "representations_complete": self.representations_complete,
            "certifications_complete": self.certifications_complete,
        }


@dataclass
class ValidationContext:
    now: datetime
    response_deadline: datetime | None = None
    set_aside_code: str | None = None
    company_facts: dict[str, Any] = field(default_factory=dict)
    package: SubmissionPackage | None = None
    supplier: dict[str, Any] = field(default_factory=dict)
    known_amendments: list[str] = field(default_factory=list)


# ── validators ──


def parse_source_deadline(date_text: str | None, time_text: str | None, tz_text: str | None) -> datetime | None:
    if not all(isinstance(value, str) and value.strip() for value in (date_text, time_text, tz_text)):
        return None
    zone = _TZ.get(tz_text) or _TZ.get(tz_text.upper()) or _TZ.get(tz_text.title())
    if zone is None:
        return None
    parsed_date = None
    cleaned = date_text.replace(",", "").replace(".", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed_date = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue
    if parsed_date is None:
        return None
    t = time_text.lower().replace(".", "").replace(" ", "")
    match = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)?$", t) or re.match(r"(\d{2})(\d{2})hours?$", t)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = match.group(3) if match.lastindex and match.lastindex >= 3 else None
    if minute > 59 or (suffix and not 1 <= hour <= 12) or (not suffix and hour > 23):
        return None
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    try:
        return parsed_date.replace(hour=hour, minute=minute, tzinfo=ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _aware(value: datetime) -> datetime:
    """Database/listing naive timestamps use UTC consistently with ingestion."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def deadline_not_passed(deadline: datetime | None, now: datetime) -> ValidatorResult:
    if deadline is None:
        return _result("deadline_not_passed", "unknown", "response deadline is not known", deadline=None, now=now)
    remaining = (_aware(deadline) - _aware(now)).total_seconds() / 3600
    if remaining <= 0:
        return _result("deadline_not_passed", "fail", "response deadline has passed", deadline=deadline, now=now, hours_remaining=round(remaining, 2))
    return _result("deadline_not_passed", "pass", "response deadline is in the future", deadline=deadline, now=now, hours_remaining=round(remaining, 2))


def deadline_timezone_consistent(key_values: dict[str, Any], opportunity_deadline: datetime | None) -> ValidatorResult:
    tz = key_values.get("deadline_timezone")
    if not tz:
        return _result("deadline_timezone", "unknown", "source does not state a deadline timezone", source=key_values)
    parsed = parse_source_deadline(key_values.get("response_deadline_date"), key_values.get("response_deadline_time"), tz)
    if parsed is None:
        return _result("deadline_timezone", "unknown", f"could not resolve source deadline with timezone {tz}", source=key_values)
    if opportunity_deadline is None:
        return _result("deadline_timezone", "pass", "source deadline and timezone parsed; no listing deadline to compare", source_deadline=parsed)
    delta = abs((parsed - _aware(opportunity_deadline)).total_seconds())
    if delta > 60:
        return _result(
            "deadline_timezone", "fail",
            f"source deadline {parsed.isoformat()} differs from listed deadline {opportunity_deadline.isoformat()} by {int(delta // 60)} minutes",
            source_deadline=parsed, listing_deadline=opportunity_deadline,
        )
    return _result("deadline_timezone", "pass", "source deadline/timezone matches the listed deadline", source_deadline=parsed, listing_deadline=opportunity_deadline)


def submission_before_deadline(package: SubmissionPackage | None, deadline: datetime | None) -> ValidatorResult:
    if package is None or package.planned_submission_at is None or deadline is None:
        return _result("submission_before_deadline", "unknown", "planned submission time or deadline not known")
    if _aware(package.planned_submission_at) >= _aware(deadline):
        return _result("submission_before_deadline", "fail", "planned submission is at or after the deadline", planned=package.planned_submission_at, deadline=deadline)
    return _result("submission_before_deadline", "pass", "planned submission precedes the deadline", planned=package.planned_submission_at, deadline=deadline)


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def required_files_present(required: list[str], package: SubmissionPackage | None, *, name: str = "required_files_present") -> ValidatorResult:
    if not required:
        return _result(name, "unknown", "no required file list is established")
    if package is None:
        return _result(name, "unknown", "submission package has not been assembled", required=required)
    present = {_norm_name(f.name) for f in package.files} | {_norm_name(f.form_id) for f in package.files if f.form_id}
    missing = [r for r in required if not any(_norm_name(r) in p for p in present)]
    if missing:
        return _result(name, "fail", f"missing: {', '.join(missing)}", required=required, missing=missing, artifact_absent=True)
    return _result(name, "pass", "all required files are in the package", required=required)


def required_forms_present(forms: list[str], package: SubmissionPackage | None) -> ValidatorResult:
    return required_files_present(forms, package, name="required_forms_present")


def signatures_confirmed(package: SubmissionPackage | None, forms: list[str] | None = None) -> ValidatorResult:
    if package is None:
        return _result("signatures_confirmed", "unknown", "submission package has not been assembled")
    targets = [f for f in package.files if f.role in {"form", "acknowledgment"}]
    if forms:
        wanted = {_norm_name(x) for x in forms}
        targets = [f for f in package.files if any(w in _norm_name(f.form_id or f.name) for w in wanted)]
    if not targets:
        return _result("signatures_confirmed", "unknown", "no file requiring signature is identified in the package", forms=forms)
    unsigned = [f.name for f in targets if f.signed is False]
    unconfirmed = [f.name for f in targets if f.signed is None]
    if unsigned:
        return _result("signatures_confirmed", "fail", f"unsigned: {', '.join(unsigned)}", unsigned=unsigned, artifact_absent=True)
    if unconfirmed:
        return _result("signatures_confirmed", "unknown", f"signature not confirmed for: {', '.join(unconfirmed)}", unconfirmed=unconfirmed)
    return _result("signatures_confirmed", "pass", "signatures explicitly confirmed", files=[f.name for f in targets])


def _amend_id(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(4) if digits else str(value)


def amendments_acknowledged(known: list[str], package: SubmissionPackage | None) -> ValidatorResult:
    known_ids = sorted({_amend_id(k) for k in known})
    if not known_ids:
        return _result("amendments_acknowledged", "pass", "no amendments are known for this solicitation", known=[])
    if package is None or package.amendments_acknowledged is None:
        return _result("amendments_acknowledged", "unknown", "amendment acknowledgments not recorded", known=known_ids)
    acknowledged = {_amend_id(a) for a in package.amendments_acknowledged}
    missing = [k for k in known_ids if k not in acknowledged]
    if missing:
        return _result("amendments_acknowledged", "fail", f"unacknowledged amendment(s): {', '.join(missing)}", known=known_ids, missing=missing, artifact_absent=True)
    return _result("amendments_acknowledged", "pass", "all known amendments acknowledged", known=known_ids)


def page_count_within_limit(limit: int | None, package: SubmissionPackage | None) -> ValidatorResult:
    if limit is None:
        return _result("page_count_within_limit", "unknown", "page limit not established")
    if package is None:
        return _result("page_count_within_limit", "unknown", "proposal not assembled", limit=limit)
    proposal = [f for f in package.files if f.role == "proposal"]
    if not proposal or any(f.page_count is None for f in proposal):
        return _result("page_count_within_limit", "unknown", "proposal page count not known", limit=limit)
    pages = sum(f.page_count or 0 for f in proposal)
    if pages > limit:
        return _result("page_count_within_limit", "fail", f"proposal has {pages} pages; limit is {limit}", pages=pages, limit=limit)
    return _result("page_count_within_limit", "pass", f"proposal has {pages} pages within limit {limit}", pages=pages, limit=limit)


def pricing_rows_populated(package: SubmissionPackage | None) -> ValidatorResult:
    if package is None or package.pricing_rows is None:
        return _result("pricing_rows_populated", "unknown", "pricing rows not provided")
    if not package.pricing_rows:
        return _result("pricing_rows_populated", "fail", "pricing workbook has no rows", artifact_absent=True)
    empty = [r.get("clin") or str(i) for i, r in enumerate(package.pricing_rows) if r.get("unit_price") in (None, "")]
    if empty:
        return _result("pricing_rows_populated", "fail", f"rows without unit price: {', '.join(empty)}", empty=empty)
    return _result("pricing_rows_populated", "pass", "every pricing row has a unit price", rows=len(package.pricing_rows))


def clins_accounted(required: dict[str, int], package: SubmissionPackage | None) -> ValidatorResult:
    if not required:
        return _result("clins_accounted", "unknown", "CLIN list not established")
    if package is None or package.pricing_rows is None:
        return _result("clins_accounted", "unknown", "pricing rows not provided", required=sorted(required))
    priced = {str(r.get("clin", "")).upper() for r in package.pricing_rows}
    missing = [c for c in sorted(required) if c.upper() not in priced]
    if missing:
        return _result("clins_accounted", "fail", f"CLIN(s) not priced: {', '.join(missing)}", missing=missing)
    return _result("clins_accounted", "pass", "all CLINs priced", clins=sorted(required))


def quantities_accounted(required: dict[str, int], package: SubmissionPackage | None) -> ValidatorResult:
    if not required:
        return _result("quantities_accounted", "unknown", "required quantities not established")
    if package is None or package.pricing_rows is None:
        return _result("quantities_accounted", "unknown", "pricing rows not provided")
    offered = {str(r.get("clin", "")).upper(): r.get("quantity") for r in package.pricing_rows}
    mismatched = {c: {"required": q, "offered": offered.get(c.upper())} for c, q in required.items() if offered.get(c.upper()) is not None and float(offered[c.upper()]) != float(q)}
    unknown = [c for c in required if offered.get(c.upper()) is None]
    if mismatched:
        return _result("quantities_accounted", "fail", f"quantity mismatch: {mismatched}", mismatched=mismatched)
    if unknown:
        return _result("quantities_accounted", "unknown", f"quantity not stated for: {', '.join(unknown)}")
    return _result("quantities_accounted", "pass", "offered quantities match", required=required)


def file_types_allowed(allowed: list[str], package: SubmissionPackage | None) -> ValidatorResult:
    if not allowed:
        return _result("file_types_allowed", "unknown", "allowed file types not established")
    if package is None or not package.files:
        return _result("file_types_allowed", "unknown", "submission package has not been assembled", allowed=allowed)
    allowed_set = {a.upper() for a in allowed}
    bad = [f.name for f in package.files if f.extension not in allowed_set]
    if bad:
        return _result("file_types_allowed", "fail", f"disallowed file type(s): {', '.join(bad)}", allowed=sorted(allowed_set), bad=bad)
    return _result("file_types_allowed", "pass", "all file types allowed", allowed=sorted(allowed_set))


def filenames_match(required_names: list[str], package: SubmissionPackage | None) -> ValidatorResult:
    if not required_names:
        return _result("filenames_match", "unknown", "required filenames not established")
    if package is None:
        return _result("filenames_match", "unknown", "submission package has not been assembled")
    names = {f.name for f in package.files}
    missing = [n for n in required_names if n not in names]
    if missing:
        return _result("filenames_match", "fail", f"required filename(s) not used exactly: {', '.join(missing)}", missing=missing)
    return _result("filenames_match", "pass", "required filenames used exactly", names=required_names)


def file_size_within_limit(max_mb: float | None, package: SubmissionPackage | None) -> ValidatorResult:
    if max_mb is None:
        return _result("file_size_within_limit", "unknown", "file size limit not established")
    if package is None or not package.files:
        return _result("file_size_within_limit", "unknown", "submission package has not been assembled")
    if any(f.size_bytes is None for f in package.files):
        return _result("file_size_within_limit", "unknown", "file size not known for every file", max_mb=max_mb)
    limit = max_mb * 1024 * 1024
    over = [f.name for f in package.files if (f.size_bytes or 0) > limit]
    if over:
        return _result("file_size_within_limit", "fail", f"file(s) over {max_mb} MB: {', '.join(over)}", over=over)
    return _result("file_size_within_limit", "pass", f"all files within {max_mb} MB", max_mb=max_mb)


def recipient_matches(required_email: str | None, package: SubmissionPackage | None) -> ValidatorResult:
    if not required_email:
        return _result("recipient_matches", "unknown", "required recipient not established")
    if package is None or not package.recipient_email:
        return _result("recipient_matches", "unknown", "package recipient not recorded", required=required_email)
    if package.recipient_email.strip().lower() != required_email.strip().lower():
        return _result("recipient_matches", "fail", f"package goes to {package.recipient_email}; instructions require {required_email}", required=required_email, actual=package.recipient_email)
    return _result("recipient_matches", "pass", "recipient matches instructions", recipient=required_email)


def portal_matches(required_portal: str | None, package: SubmissionPackage | None) -> ValidatorResult:
    if not required_portal:
        return _result("portal_matches", "unknown", "required portal not established")
    if package is None or not (package.portal or package.submission_method):
        return _result("portal_matches", "unknown", "package destination not recorded", required=required_portal)
    actual = f"{package.portal or ''} {package.submission_method or ''}".lower()
    if required_portal.lower() not in actual:
        return _result("portal_matches", "fail", f"package destination {actual.strip()} does not match {required_portal}", required=required_portal)
    return _result("portal_matches", "pass", "destination matches instructions", portal=required_portal)


def sam_registration_known(facts: dict[str, Any], deadline: datetime | None) -> ValidatorResult:
    status = (facts.get("sam_registration_status") or "").strip().lower()
    if not status:
        return _result("sam_registration_known", "unknown", "SAM registration status is not in company facts")
    if status != "active":
        return _result("sam_registration_known", "fail", f"SAM registration status is {status}", status=status)
    expires = facts.get("sam_expiration_date")
    if expires:
        expiry = datetime.fromisoformat(str(expires)[:10])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry.date() < datetime.now(UTC).date():
            return _result("sam_registration_known", "fail", "SAM registration is expired", expires=expiry.date())
        if deadline is not None and expiry < deadline:
            return _result("sam_registration_known", "fail", "SAM registration expires before the response deadline", expires=expiry, deadline=deadline)
    return _result("sam_registration_known", "pass", "SAM registration active", status=status, expires=expires)


UNRESTRICTED_SET_ASIDE_CODES = frozenset({"N", "NONE", "UNRESTRICTED"})


def set_aside_matches(set_aside_code: str | None, facts: dict[str, Any]) -> ValidatorResult:
    if not set_aside_code or set_aside_code.strip().upper() in UNRESTRICTED_SET_ASIDE_CODES:
        return _result("set_aside_matches", "pass", "no set-aside on this opportunity")
    required = SET_ASIDE_STATUS.get(set_aside_code.strip().upper())
    if required is None:
        return _result("set_aside_matches", "unknown", f"set-aside code {set_aside_code} is not mapped to a business status", code=set_aside_code)
    statuses = facts.get("socioeconomic") or {}
    value = statuses.get(required)
    if value is True:
        return _result("set_aside_matches", "pass", f"company facts assert {required}", code=set_aside_code, status=required)
    if value is False:
        return _result("set_aside_matches", "fail", f"company facts state the company is not {required}", code=set_aside_code, status=required)
    return _result("set_aside_matches", "unknown", f"company facts do not establish {required} status", code=set_aside_code, status=required)


def delivery_date_arithmetic(required_days: int | None, supplier: dict[str, Any]) -> ValidatorResult:
    lead = supplier.get("lead_time_days")
    transit = supplier.get("transit_days")
    if required_days is None:
        return _result("delivery_date_arithmetic", "unknown", "required delivery period not established")
    if lead is None:
        return _result("delivery_date_arithmetic", "unknown", "supplier lead time not documented", required_days=required_days)
    if transit is None:
        return _result(
            "delivery_date_arithmetic", "unknown",
            f"supplier lead time {lead} days documented but transit time is not; cannot confirm {required_days}-day delivery",
            required_days=required_days, lead_time_days=lead,
        )
    total = int(lead) + int(transit)
    if total > required_days:
        return _result("delivery_date_arithmetic", "fail", f"lead {lead} + transit {transit} = {total} days exceeds {required_days}", required_days=required_days, total_days=total)
    return _result("delivery_date_arithmetic", "pass", f"lead {lead} + transit {transit} = {total} days within {required_days}", required_days=required_days, total_days=total)


def margin_arithmetic(price: float | None, cost: float | None, min_margin_pct: float | None = None) -> ValidatorResult:
    if price is None or cost is None:
        return _result("margin_arithmetic", "unknown", "price or cost not established")
    if cost <= 0:
        return _result("margin_arithmetic", "unknown", "cost must be positive to compute margin", cost=cost)
    margin = round((price - cost) / cost * 100, 1)
    if price <= cost:
        return _result("margin_arithmetic", "fail", f"price {price} does not exceed cost {cost}", margin_pct=margin)
    if min_margin_pct is not None and margin < min_margin_pct:
        return _result("margin_arithmetic", "fail", f"margin {margin}% below minimum {min_margin_pct}%", margin_pct=margin)
    return _result("margin_arithmetic", "pass", f"margin {margin}%", margin_pct=margin)


# ── requirement mapping ──


def validators_for(requirement_type: str | None, key_values: dict[str, Any], text: str, ctx: ValidationContext) -> list[ValidatorResult]:
    """Run every validator that applies to one requirement."""
    kv = key_values or {}
    results: list[ValidatorResult] = []
    lowered = (text or "").lower()
    package = ctx.package
    if "response_deadline_date" in kv or (requirement_type == "submission" and re.search(r"\b(due|no later than|deadline|received by)\b", lowered)):
        source_deadline = parse_source_deadline(kv.get("response_deadline_date"), kv.get("response_deadline_time"), kv.get("deadline_timezone"))
        deadline = ctx.response_deadline or source_deadline
        results.append(deadline_not_passed(deadline, ctx.now))
        results.append(deadline_timezone_consistent(kv, ctx.response_deadline))
        if package is not None and package.planned_submission_at is not None:
            results.append(submission_before_deadline(package, deadline))
    if "page_limit" in kv:
        results.append(page_count_within_limit(int(kv["page_limit"]), package))
    forms = kv.get("forms") or []
    if forms and requirement_type in {"administrative", "signature", "submission", "representation", "certification"}:
        results.append(required_forms_present(forms, package))
    if requirement_type == "signature":
        results.append(signatures_confirmed(package, forms or None))
    if requirement_type == "amendment_acknowledgment" or "amendments_to_acknowledge" in kv:
        known = sorted(set(ctx.known_amendments) | set(kv.get("amendments_to_acknowledge") or []))
        results.append(amendments_acknowledged(known, package))
    if kv.get("clin_quantities"):
        results.append(clins_accounted(kv["clin_quantities"], package))
        results.append(quantities_accounted(kv["clin_quantities"], package))
    elif requirement_type == "pricing" and re.search(r"\b(all|each|every)\b[^.]{0,40}\b(clin|line item|row)s?\b", lowered):
        results.append(pricing_rows_populated(package))
    if kv.get("allowed_file_types"):
        results.append(file_types_allowed(kv["allowed_file_types"], package))
    if kv.get("max_file_size_mb") is not None:
        results.append(file_size_within_limit(float(kv["max_file_size_mb"]), package))
    if kv.get("required_filenames"):
        results.append(filenames_match(kv["required_filenames"], package))
    if kv.get("recipient_email"):
        results.append(recipient_matches(kv["recipient_email"], package))
    if kv.get("submission_portal"):
        results.append(portal_matches(kv["submission_portal"], package))
    if requirement_type == "set_aside":
        results.append(set_aside_matches(ctx.set_aside_code, ctx.company_facts))
    if re.search(r"system for award management|\bsam\b registration|registered in sam|52\.204-7\b", lowered):
        results.append(sam_registration_known(ctx.company_facts, ctx.response_deadline))
    if kv.get("delivery_days") is not None:
        results.append(delivery_date_arithmetic(int(kv["delivery_days"]), ctx.supplier))
    return results


def build_context(
    opportunity: Opportunity,
    inventory: Inventory,
    *,
    company_facts: dict[str, Any] | None = None,
    package: SubmissionPackage | None = None,
    supplier: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ValidationContext:
    return ValidationContext(
        now=now or datetime.now(UTC),
        response_deadline=opportunity.response_deadline,
        set_aside_code=opportunity.set_aside_code,
        company_facts=company_facts or {},
        package=package,
        supplier=supplier or {},
        known_amendments=[f"{d.amendment_number:04d}" for d in inventory.amendments() if d.amendment_number],
    )


def run_deterministic_validation(session: Session, opportunity_id: int, ctx: ValidationContext) -> dict[str, Any]:
    requirements: list[Requirement] = active_requirements(session, opportunity_id)
    summary: dict[str, int] = {"pass": 0, "fail": 0, "unknown": 0}
    per_requirement: dict[int, list[dict[str, Any]]] = {}
    for req in requirements:
        results = [r.as_dict() for r in validators_for(req.requirement_type, req.key_values or {}, f"{req.requirement_text} {req.source_quote or ''}", ctx)]
        validation = dict(req.validation or {})
        validation["deterministic"] = results
        req.validation = validation
        per_requirement[req.id] = results
        for r in results:
            summary[r["status"]] += 1
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="deterministic_validation",
        run_version=RUN_VERSION,
        output={
            "summary": summary,
            "results": {str(k): v for k, v in per_requirement.items() if v},
            "context": {
                "now": ctx.now.isoformat(),
                "response_deadline": ctx.response_deadline.isoformat() if ctx.response_deadline else None,
                "company_facts_keys": sorted(ctx.company_facts),
                "package": ctx.package.manifest() if ctx.package else None,
                "supplier": ctx.supplier,
                "known_amendments": ctx.known_amendments,
            },
        },
    )
    return {"run_id": run.id, "summary": summary}
