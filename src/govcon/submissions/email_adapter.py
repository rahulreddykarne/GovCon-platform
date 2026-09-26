"""Submission email draft generation (Phase 11).

Drafts a submission email from the assembled submission package. The draft
is human-editable and must be sent manually; the platform does not
autonomously send submission emails.

No secrets are embedded in the draft.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import (
    Opportunity,
    Proposal,
    ProposalVersion,
    Submission,
)


def draft_submission_email(
    session: Session,
    *,
    opportunity_id: int,
) -> dict[str, Any]:
    """Draft a submission email from the assembled submission package.

    Returns a dict with keys:
    - to: recipient email
    - subject: suggested subject line
    - body: plain-text email body
    - attachments: list of suggested attachment filenames
    - notes: list of human-review notes
    """
    opp = session.get(Opportunity, opportunity_id)
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()

    sol_num = (opp.solicitation_number or f"OPP-{opportunity_id}") if opp else f"OPP-{opportunity_id}"
    title = (opp.title or "Government Contract Solicitation") if opp else "Government Contract Solicitation"

    to = (submission.recipient_email or "") if submission else ""
    notes: list[str] = []

    if not to:
        notes.append("[[REVIEW: Recipient email not determined — check solicitation for submission address]]")

    subject = f"Quote/Proposal in Response to Solicitation {sol_num}"

    current_version_num: str = "?"
    attachments: list[str] = []
    if proposal and proposal.current_version_id:
        pv = session.get(ProposalVersion, proposal.current_version_id)
        if pv:
            current_version_num = str(pv.version_number)
    attachments.append(f"proposal_v{current_version_num}.docx")

    if submission and submission.required_files:
        rf = submission.required_files
        files = rf.get("files", []) if isinstance(rf, dict) else (rf or [])
        for f in files:
            if f not in attachments:
                attachments.append(f)

    # Build body
    agency = (opp.agency_path or "The Contracting Officer") if opp else "The Contracting Officer"
    body_lines = [
        f"To: {to or '[[INSERT RECIPIENT]]'}",
        f"Subject: {subject}",
        "",
        f"Dear {agency},",
        "",
        f"Please find attached our quote/proposal in response to Solicitation Number {sol_num}: {title}.",
        "",
        "Attachments:",
    ]
    for att in attachments:
        body_lines.append(f"  - {att}")

    if submission and submission.required_actions:
        acks = (submission.required_actions or {}).get("amendment_acknowledgments", [])
        if acks:
            body_lines.append("")
            body_lines.append(f"We acknowledge Amendment(s): {', '.join(acks)}.")

    body_lines += [
        "",
        "[[REVIEW: Insert any required representations, certifications, or company identifiers here.]]",
        "",
        "Respectfully submitted,",
        "[[INSERT AUTHORIZED SIGNATURE / NAME / TITLE / DATE]]",
        "",
        "NOTE: This is a system-generated draft. Review all content before sending.",
        "The platform does NOT automatically submit this email.",
    ]

    return {
        "to": to,
        "subject": subject,
        "body": "\n".join(body_lines),
        "attachments": attachments,
        "notes": notes,
    }
