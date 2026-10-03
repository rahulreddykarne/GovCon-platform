"""Validated, outcome-specific human feedback inputs."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Margin = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    government_feedback: str | None = None
    debrief_notes: str | None = None
    lessons_learned: str | None = None


class AwardFeedback(Feedback):
    awarded_vendor_uei: str | None = None
    awarded_vendor_name: str | None = None
    award_amount: Amount | None = None
    award_date: date | None = None


class WonFeedback(AwardFeedback):
    outcome: Literal["won"]
    win_reason: str | None = None
    win_margin_pct: Margin | None = None
    win_supplier: str | None = None
    win_delivery_terms: str | None = None
    win_proposal_version: str | None = None


class LostFeedback(AwardFeedback):
    outcome: Literal["lost"]
    loss_reason: str | None = None
    known_winning_price: Amount | None = None


class NoBidFeedback(Feedback):
    outcome: Literal["no_bid"]
    no_bid_reason: str | None = None
    no_bid_category: Literal["missing_capability", "margin", "deadline", "supplier_availability", "eligibility", "compliance_issue", "competition", "strategic_choice", "other"] | None = None


class CancelledFeedback(Feedback):
    outcome: Literal["cancelled"]


OUTCOME_SCHEMAS = {"won": WonFeedback, "lost": LostFeedback, "no_bid": NoBidFeedback, "cancelled": CancelledFeedback}
