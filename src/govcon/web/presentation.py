"""Small presentation models and human-readable notification summaries."""

from dataclasses import dataclass
from typing import Any

from govcon.models import Notification


@dataclass(frozen=True)
class ViewRow:
    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_NOTICES = {
    "review_assigned": ("You have been assigned to review this opportunity.", "review"),
    "reviewer_completed": ("A reviewer completed their assignment; check the review decision.", "review"),
    "new_reviewer_comment": ("A reviewer added a comment to this opportunity.", "review"),
    "ai_flagged_comment_needs_evidence": ("Your comment needs supporting evidence for the AI check.", "review"),
    "review_quorum_satisfied": ("The required reviews are complete; an approver can decide.", "review"),
    "second_review_required": ("This opportunity requires a second review.", "review"),
    "second_review_requested": ("A reviewer requested a second review.", "review"),
    "review_reassigned": ("The reviewer assignments have changed.", "review"),
    "approval_pending": ("The review is ready for an approver's decision.", "review"),
    "material_amendment_after_review": ("The solicitation changed materially; review the updated evidence.", "review"),
    "proposal_package_generated": ("The proposal package is ready to inspect.", "submission"),
    "submission_ready": ("The submission package is ready for the final checks.", "submission"),
    "review_reminder": ("Your assigned review is due soon.", "review"),
    "review_overdue_escalation": ("A required review is overdue; check the assignments.", "review"),
    "deadline_escalation": ("The response deadline is approaching; check the remaining review work.", "review"),
    "auto_pursued": ("This opportunity was pursued automatically under the configured policy.", "overview"),
    "registration_expiring": ("The company's SAM registration is approaching expiry; check its registration.", None),
    "outcome_suggested": ("An award may match this pursuit; confirm or dismiss the suggested outcome.", "submission"),
}


def notification_summary(row: Notification) -> tuple[str, str]:
    sentence, tab = _NOTICES.get(row.notification_type, ("A new workspace update is available.", "overview"))
    target = f"/workspace/{row.opportunity_id}?tab={tab}" if tab and row.opportunity_id else "/admin?section=settings"
    return sentence, target
