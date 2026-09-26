"""Decision-bundle metadata and JEV question maps."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from govcon.config import Settings, get_settings


@dataclass(frozen=True)
class BundleDefinition:
    name: str
    version: str
    decision_spec_file: str
    input_schema: str
    high_consequence: bool
    jev_questions: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class DecisionSpecMetadata:
    bundle_name: str
    path: Path
    decision_spec_name: str
    version: str
    content_hash: str


_QUESTION_SETS: dict[str, dict[str, dict[str, Any]]] = {
    "opportunity_triage": {
        "relevance": {
            "type": "choice",
            "instructions": "Classify relevance for the business.",
            "criteria": {
                "relevant": "Likely aligned with capability/targets.",
                "not_relevant": "Clearly not aligned.",
                "review": "Insufficient or conflicting information.",
            },
        },
        "priority": {
            "type": "choice",
            "instructions": "Set operational priority.",
            "criteria": {
                "low": "Can wait.",
                "medium": "Normal queue.",
                "high": "Needs fast handling.",
                "critical": "Immediate action needed.",
            },
        },
        "human_review_required": {
            "type": "noul",
            "instructions": "Does this require immediate human review?",
        },
    },
    "eligibility_and_execution": {
        "capability_match": {
            "type": "choice",
            "instructions": "Assess capability fit strength.",
            "criteria": {
                "very_low": "Capability fit is weak.",
                "low": "Limited fit.",
                "medium": "Partial fit.",
                "high": "Strong fit.",
                "very_high": "Excellent fit.",
            },
        },
        "delivery_feasibility": {
            "type": "choice",
            "instructions": "Assess delivery feasibility.",
            "criteria": {
                "yes": "Delivery appears feasible.",
                "no": "Delivery appears infeasible.",
                "review": "Not enough evidence.",
            },
        },
        "human_review_required": {
            "type": "noul",
            "instructions": "Is human review required for eligibility/execution?",
        },
    },
    "sourcing_and_supplier": {
        "supplier_availability": {
            "type": "choice",
            "instructions": "Assess supplier availability sufficiency.",
            "criteria": {"yes": "Sufficient", "no": "Insufficient", "review": "Unclear"},
        },
        "lead_time_fit": {
            "type": "choice",
            "instructions": "Assess supplier lead-time fit.",
            "criteria": {"yes": "Fits", "no": "Does not fit", "risky": "Borderline risk"},
        },
    },
    "market_and_pricing": {
        "historical_comparability": {
            "type": "choice",
            "instructions": "Assess historical comparability quality.",
            "criteria": {"low": "Weak", "medium": "Moderate", "high": "Strong"},
        },
        "margin_quality": {
            "type": "choice",
            "instructions": "Assess expected margin quality.",
            "criteria": {
                "poor": "Unattractive economics",
                "marginal": "Borderline economics",
                "good": "Healthy economics",
                "strong": "Very attractive economics",
            },
        },
    },
    "bid_decision": {
        "recommendation": {
            "type": "choice",
            "instructions": "Recommend bid/no-bid/review using supplied state only.",
            "criteria": {
                "bid": "Proceed toward bid subject to human approval.",
                "no_bid": "Recommend no-bid.",
                "review": "Escalate for human review.",
            },
        },
        "evidence_confidence": {
            "type": "choice",
            "instructions": "Assess confidence in available evidence.",
            "criteria": {"low": "Low", "medium": "Medium", "high": "High"},
        },
        "human_review_required": {
            "type": "noul",
            "instructions": "Is human review required before further progression?",
        },
    },
    "compliance_and_amendment": {
        "amendment_material": {
            "type": "noul",
            "instructions": "Is the amendment materially impactful?",
        },
        "proposal_revision_required": {
            "type": "noul",
            "instructions": "Is proposal revision required?",
        },
        "bid_reassessment_required": {
            "type": "noul",
            "instructions": "Must bid/no-bid be reassessed?",
        },
    },
    "collaborative_review_synthesis": {
        "recommendation": {
            "type": "choice",
            "instructions": "Recommend next review posture.",
            "criteria": {
                "bid": "Proceed toward approval gate.",
                "no_bid": "Recommend no-bid posture.",
                "review": "Further review needed.",
            },
        },
        "evidence_confidence": {
            "type": "choice",
            "instructions": "Confidence in synthesized review evidence.",
            "criteria": {"low": "Low", "medium": "Medium", "high": "High"},
        },
    },
    "proposal_review": {
        "quality": {
            "type": "choice",
            "instructions": "Assess proposal section quality.",
            "criteria": {"poor": "Poor", "fair": "Fair", "good": "Good", "strong": "Strong"},
        },
        "ready_for_human_review": {
            "type": "noul",
            "instructions": "Is this ready for human review?",
        },
    },
    "submission_readiness": {
        "status": {
            "type": "choice",
            "instructions": "Assess submission readiness state.",
            "criteria": {
                "ready": "Submission appears ready.",
                "not_ready": "Submission is not ready.",
                "review": "Requires human review.",
            },
        },
        "human_verification_required": {
            "type": "noul",
            "instructions": "Is manual verification required?",
        },
    },
    "post_submission_routing": {
        "action_required": {
            "type": "noul",
            "instructions": "Does this communication require action?",
        },
        "urgency": {
            "type": "choice",
            "instructions": "Assess urgency.",
            "criteria": {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"},
        },
    },
    "outcome_learning": {
        "similarity_relevance": {
            "type": "choice",
            "instructions": "How relevant is this outcome to future opportunities?",
            "criteria": {"low": "Low", "medium": "Medium", "high": "High"},
        },
        "use_for_future_analysis": {
            "type": "noul",
            "instructions": "Should this outcome feed future analysis?",
        },
    },
    "model_router": {
        "generative_llm_required": {
            "type": "noul",
            "instructions": "Is a generative model required at all?",
        },
        "high_capability_model_required": {
            "type": "noul",
            "instructions": "Does this require a high-capability model?",
        },
    },
    "workflow_router": {
        "next_action": {
            "type": "choice",
            "instructions": "Choose the safest next workflow action.",
            "criteria": {
                "dismiss": "Drop this item.",
                "analyze": "Run additional AI/analysis.",
                "review": "Route to human review.",
                "pursue": "Proceed in workflow.",
                "wait": "Defer pending new evidence.",
            },
        },
        "user_attention_required": {
            "type": "noul",
            "instructions": "Is immediate user attention required?",
        },
    },
}

_BUNDLES: dict[str, BundleDefinition] = {
    "opportunity_triage": BundleDefinition(
        name="opportunity_triage",
        version="v1",
        decision_spec_file="opportunity_triage_v1.yaml",
        input_schema="opportunity_triage_state.v1",
        high_consequence=False,
        jev_questions=_QUESTION_SETS["opportunity_triage"],
    ),
    "eligibility_and_execution": BundleDefinition(
        name="eligibility_and_execution",
        version="v1",
        decision_spec_file="eligibility_execution_v1.yaml",
        input_schema="eligibility_execution_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["eligibility_and_execution"],
    ),
    "sourcing_and_supplier": BundleDefinition(
        name="sourcing_and_supplier",
        version="v1",
        decision_spec_file="sourcing_supplier_v1.yaml",
        input_schema="sourcing_supplier_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["sourcing_and_supplier"],
    ),
    "market_and_pricing": BundleDefinition(
        name="market_and_pricing",
        version="v1",
        decision_spec_file="market_pricing_v1.yaml",
        input_schema="market_pricing_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["market_and_pricing"],
    ),
    "bid_decision": BundleDefinition(
        name="bid_decision",
        version="v1",
        decision_spec_file="bid_decision_v1.yaml",
        input_schema="bid_decision_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["bid_decision"],
    ),
    "compliance_and_amendment": BundleDefinition(
        name="compliance_and_amendment",
        version="v1",
        decision_spec_file="compliance_amendment_v1.yaml",
        input_schema="compliance_amendment_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["compliance_and_amendment"],
    ),
    "collaborative_review_synthesis": BundleDefinition(
        name="collaborative_review_synthesis",
        version="v1",
        decision_spec_file="collaborative_review_v1.yaml",
        input_schema="collaborative_review_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["collaborative_review_synthesis"],
    ),
    "proposal_review": BundleDefinition(
        name="proposal_review",
        version="v1",
        decision_spec_file="proposal_review_v1.yaml",
        input_schema="proposal_review_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["proposal_review"],
    ),
    "submission_readiness": BundleDefinition(
        name="submission_readiness",
        version="v1",
        decision_spec_file="submission_readiness_v1.yaml",
        input_schema="submission_readiness_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["submission_readiness"],
    ),
    "post_submission_routing": BundleDefinition(
        name="post_submission_routing",
        version="v1",
        decision_spec_file="post_submission_routing_v1.yaml",
        input_schema="post_submission_routing_state.v1",
        high_consequence=True,
        jev_questions=_QUESTION_SETS["post_submission_routing"],
    ),
    "outcome_learning": BundleDefinition(
        name="outcome_learning",
        version="v1",
        decision_spec_file="outcome_learning_v1.yaml",
        input_schema="outcome_learning_state.v1",
        high_consequence=False,
        jev_questions=_QUESTION_SETS["outcome_learning"],
    ),
    "model_router": BundleDefinition(
        name="model_router",
        version="v1",
        decision_spec_file="model_router_v1.yaml",
        input_schema="model_router_state.v1",
        high_consequence=False,
        jev_questions=_QUESTION_SETS["model_router"],
    ),
    "workflow_router": BundleDefinition(
        name="workflow_router",
        version="v1",
        decision_spec_file="workflow_router_v1.yaml",
        input_schema="workflow_router_state.v1",
        high_consequence=False,
        jev_questions=_QUESTION_SETS["workflow_router"],
    ),
}

_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def all_bundle_names() -> tuple[str, ...]:
    return tuple(_BUNDLES)


def bundle_definition(bundle_name: str) -> BundleDefinition:
    try:
        return _BUNDLES[bundle_name]
    except KeyError as exc:
        raise ValueError(f"unknown decision bundle: {bundle_name}") from exc


def decision_spec_path(bundle_name: str, *, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    definition = bundle_definition(bundle_name)
    return settings.resolved_prompt_root() / "jev" / definition.decision_spec_file


def load_decision_spec_metadata(
    bundle_name: str, *, settings: Settings | None = None
) -> DecisionSpecMetadata:
    path = decision_spec_path(bundle_name, settings=settings)
    text = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    name_match = _NAME_RE.search(text)
    version_match = _VERSION_RE.search(text)
    fallback = bundle_definition(bundle_name)
    return DecisionSpecMetadata(
        bundle_name=bundle_name,
        path=path,
        decision_spec_name=name_match.group(1) if name_match else fallback.name,
        version=version_match.group(1) if version_match else fallback.version,
        content_hash=content_hash,
    )
