"""Pydantic models for AI analysis output validation.

Every structured AI analysis output must be validated through these schemas
before persistence. Malformed output fails closed — it is never silently
stored as valid.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None


class LineItem(BaseModel):
    description: str | None = None
    manufacturer: str | None = None
    part_number: str | None = None
    nsn: str | None = None
    quantity: int | float | None = None
    unit: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class DeliveryInfo(BaseModel):
    location: str | None = None
    required_date: str | None = None
    delivery_days: int | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class KeyDate(BaseModel):
    label: str
    date: str | None = None
    timezone: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class SubmissionInfo(BaseModel):
    method: str | None = None
    portal: str | None = None
    recipient_email: str | None = None
    deadline: str | None = None
    timezone: str | None = None
    required_files: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class EligibilityInfo(BaseModel):
    set_aside: str | None = None
    small_business_size_standard: str | None = None
    other_restrictions: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class EvaluationFactor(BaseModel):
    name: str
    weight: str | None = None
    description: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class PricingStructure(BaseModel):
    format: str | None = None
    instructions: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class AmendmentStatus(BaseModel):
    amendment_count: int | None = None
    latest_amendment: str | None = None
    summary: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class RiskFlag(BaseModel):
    category: str
    description: str
    severity: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class MissingInfo(BaseModel):
    field: str
    reason: str
    impact: str | None = None


class SolicitationAnalysisV1(BaseModel):
    """Schema for solicitation_analysis.v1 output."""

    summary: str | None = None
    items: list[LineItem] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)
    delivery: DeliveryInfo | None = None
    eligibility: EligibilityInfo | None = None
    evaluation_factors: list[EvaluationFactor] = Field(default_factory=list)
    past_performance_requirements: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    country_of_origin_references: list[str] = Field(default_factory=list)
    submission: SubmissionInfo | None = None
    pricing_structure: PricingStructure | None = None
    amendment_status: AmendmentStatus | None = None
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[MissingInfo] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    clauses: list[str] = Field(default_factory=list)


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "solicitation_analysis.v1": SolicitationAnalysisV1,
}

from govcon.compliance.schemas import COMPLIANCE_SCHEMAS  # noqa: E402

SCHEMA_REGISTRY.update(COMPLIANCE_SCHEMAS)


def validate_analysis_output(schema_version: str, data: dict) -> BaseModel:
    """Validate AI output against its declared schema version.

    Raises ``ValueError`` when the schema version is unknown.
    Raises ``pydantic.ValidationError`` when the data does not conform.
    """
    schema_cls = SCHEMA_REGISTRY.get(schema_version)
    if schema_cls is None:
        raise ValueError(f"unknown analysis schema version: {schema_version}")
    return schema_cls.model_validate(data)
