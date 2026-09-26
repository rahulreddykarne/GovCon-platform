"""Proposal and submission package export (Phase 11).

Exports proposal versions to DOCX, and the full submission package to a ZIP
archive. PDF export is delegated to a future phase; for now a plain-text
fallback is written. XLSX export of requirement coverage is also included.

All export functions are intentionally pure I/O — they do not mutate DB rows.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import (
    ComplianceFinding,
    Opportunity,
    Proposal,
    ProposalSection,
    ProposalVersion,
    Requirement,
    Submission,
)
from govcon.proposals.versions import get_sections_for_version


def export_proposal_docx(
    session: Session,
    *,
    proposal_version_id: int,
) -> bytes:
    """Export a proposal version to DOCX bytes.

    Uses python-docx. Falls back to a plain-text representation if python-docx
    is unavailable (should not happen; it is a declared dependency).
    """
    pv = session.get(ProposalVersion, proposal_version_id)
    if pv is None:
        raise ValueError(f"ProposalVersion {proposal_version_id} not found")

    sections = get_sections_for_version(session, proposal_version_id)

    proposal = session.get(Proposal, pv.proposal_id)
    opp = session.get(Opportunity, proposal.opportunity_id) if proposal else None
    title = (opp.title or f"Proposal #{pv.proposal_id}") if opp else f"Proposal #{pv.proposal_id}"

    try:
        from docx import Document
        from docx.shared import Pt

        doc = Document()
        doc.add_heading(title, 0)
        doc.add_paragraph(f"Version {pv.version_number} — {pv.created_at.date() if pv.created_at else 'draft'}")
        doc.add_paragraph(f"Prepared by: {pv.created_by}")

        for section in sections:
            heading_text = section.heading or (section.section_key or "Section").replace("_", " ").title()
            doc.add_heading(heading_text, level=1)
            content = section.content or ""
            if section.requirement_ids:
                content += f"\n\n[Addresses requirements: {', '.join(str(r) for r in section.requirement_ids)}]"
            doc.add_paragraph(content)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    except ImportError:
        return _export_proposal_text(title, pv, sections).encode("utf-8")


def _export_proposal_text(
    title: str,
    pv: ProposalVersion,
    sections: list[ProposalSection],
) -> str:
    lines = [title, "=" * len(title), f"Version {pv.version_number}", ""]
    for s in sections:
        heading = s.heading or (s.section_key or "Section").replace("_", " ").title()
        lines.append(heading)
        lines.append("-" * len(heading))
        lines.append(s.content or "")
        if s.requirement_ids:
            lines.append(f"\n[Requirements: {', '.join(str(r) for r in s.requirement_ids)}]")
        lines.append("")
    return "\n".join(lines)


def export_coverage_xlsx(
    session: Session,
    *,
    opportunity_id: int,
    proposal_version_id: int,
) -> bytes:
    """Export requirement-to-proposal coverage as XLSX."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    requirements = list(
        session.scalars(
            select(Requirement).where(Requirement.opportunity_id == opportunity_id)
        ).all()
    )
    sections = get_sections_for_version(session, proposal_version_id)
    section_map = {s.section_key: s for s in sections}

    wb = Workbook()
    ws = wb.active
    ws.title = "Coverage"

    headers = [
        "Req ID", "Type", "Mandatory", "Severity", "Status",
        "Assigned Section", "Requirement Text", "Blocks Submission"
    ]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True)

    STATUS_COLORS = {
        "satisfied": "C6EFCE",
        "missing": "FFC7CE",
        "unknown": "FFEB9C",
        "needs_review": "FFEB9C",
        "stale": "D9D9D9",
        "not_applicable": "EDEDED",
    }

    for row_idx, req in enumerate(requirements, 2):
        fill_color = STATUS_COLORS.get(req.status or "unknown", "FFFFFF")
        fill = PatternFill("solid", fgColor=fill_color)
        row_data = [
            req.id,
            req.requirement_type or "",
            "Yes" if req.mandatory else "No",
            req.severity or "",
            req.status or "unreviewed",
            req.assigned_proposal_section or "",
            (req.requirement_text or "")[:200],
            "Yes" if req.blocks_submission else "No",
        ]
        for col, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            cell.fill = fill

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_submission_zip(
    session: Session,
    *,
    opportunity_id: int,
    include_proposal_docx: bool = True,
    include_coverage_xlsx: bool = True,
) -> bytes:
    """Assemble a submission ZIP package.

    Contents:
    - proposal_vN.docx (current version)
    - coverage_matrix.xlsx
    - submission_instructions.txt
    - file_manifest.json
    - submission_checklist.json (from submissions.checklist)
    """
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    opp = session.get(Opportunity, opportunity_id)
    sol_num = (opp.solicitation_number or str(opportunity_id)) if opp else str(opportunity_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest: dict[str, Any] = {
            "opportunity_id": opportunity_id,
            "solicitation_number": sol_num,
            "files": [],
        }

        if proposal and proposal.current_version_id and include_proposal_docx:
            pv = session.get(ProposalVersion, proposal.current_version_id)
            if pv:
                docx_bytes = export_proposal_docx(session, proposal_version_id=pv.id)
                fname = f"proposal_v{pv.version_number}.docx"
                zf.writestr(fname, docx_bytes)
                manifest["files"].append({"filename": fname, "type": "proposal_docx"})

        if include_coverage_xlsx and proposal and proposal.current_version_id:
            try:
                xlsx_bytes = export_coverage_xlsx(
                    session,
                    opportunity_id=opportunity_id,
                    proposal_version_id=proposal.current_version_id,
                )
                fname = "coverage_matrix.xlsx"
                zf.writestr(fname, xlsx_bytes)
                manifest["files"].append({"filename": fname, "type": "coverage_xlsx"})
            except Exception:
                pass

        # Submission instructions
        instructions = _build_instructions_text(opp, submission)
        zf.writestr("submission_instructions.txt", instructions)
        manifest["files"].append({"filename": "submission_instructions.txt", "type": "instructions"})

        # Checklist
        from govcon.submissions.checklist import generate_final_checklist
        checklist = generate_final_checklist(session, opportunity_id=opportunity_id)
        zf.writestr("submission_checklist.json", json.dumps(checklist, indent=2, default=str))
        manifest["files"].append({"filename": "submission_checklist.json", "type": "checklist"})

        zf.writestr("file_manifest.json", json.dumps(manifest, indent=2, default=str))

    return buf.getvalue()


def _build_instructions_text(
    opp: Opportunity | None,
    submission: Submission | None,
) -> str:
    lines = ["SUBMISSION INSTRUCTIONS", "=" * 24, ""]
    if opp:
        lines.append(f"Solicitation: {opp.solicitation_number or 'Unknown'}")
        lines.append(f"Title: {opp.title or 'Unknown'}")
        lines.append("")
    if submission:
        lines.append(f"Method: {submission.submission_method or 'Unknown'}")
        lines.append(f"Destination: {submission.submission_destination or 'Unknown'}")
        lines.append(f"Portal: {submission.portal_name or 'N/A'}")
        lines.append(f"Portal URL: {submission.portal_url or 'N/A'}")
        lines.append(f"Recipient: {submission.recipient_email or 'N/A'}")
        if submission.submission_deadline:
            lines.append(
                f"Deadline: {submission.submission_deadline.isoformat()} "
                f"({submission.deadline_timezone or 'UTC'})"
            )
        if submission.required_files:
            lines.append("\nRequired Files:")
            files = submission.required_files
            if isinstance(files, dict):
                for f in files.get("files", []):
                    lines.append(f"  - {f}")
            elif isinstance(files, list):
                for f in files:
                    lines.append(f"  - {f}")
    else:
        lines.append("[BLOCKER: No submission record — submission instructions not yet generated]")
    lines.append("")
    lines.append("IMPORTANT: The platform does NOT auto-submit.")
    lines.append("The authorized user must complete submission manually.")
    return "\n".join(lines)
