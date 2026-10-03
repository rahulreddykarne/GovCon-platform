"""Proposal and submission package export (Phase 11).

Exports proposal versions to DOCX/PDF and assembled supporting files to a ZIP
archive, with an XLSX coverage matrix and SHA-256 file manifest.

All export functions are intentionally pure I/O — they do not mutate DB rows.
"""

from __future__ import annotations

import hashlib
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

    Uses python-docx; failure to produce the requested format is an error.
    The exported bytes' hash is recorded as an artifact of this version, so a
    package may carry this file as the proposal.
    """
    pv = session.get(ProposalVersion, proposal_version_id)
    if pv is None:
        raise ValueError(f"ProposalVersion {proposal_version_id} not found")

    sections = get_sections_for_version(session, proposal_version_id)

    proposal = session.get(Proposal, pv.proposal_id)
    opp = session.get(Opportunity, proposal.opportunity_id) if proposal else None
    title = (opp.title or f"Proposal #{pv.proposal_id}") if opp else f"Proposal #{pv.proposal_id}"

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
    data = buf.getvalue()
    _record_export(session, pv, proposal, data, "docx")
    return data


def export_proposal_pdf(session: Session, *, proposal_version_id: int) -> bytes:
    """Render proposal text with automatic wrapping and pagination."""
    from xml.sax.saxutils import escape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    pv = session.get(ProposalVersion, proposal_version_id)
    if pv is None:
        raise ValueError(f"ProposalVersion {proposal_version_id} not found")
    proposal = session.get(Proposal, pv.proposal_id)
    opp = session.get(Opportunity, proposal.opportunity_id)
    styles = getSampleStyleSheet()
    story = [Paragraph(escape(opp.title or "Proposal"), styles["Title"]), Paragraph(f"Version {pv.version_number}", styles["Normal"]), Spacer(1, 12)]
    for section in get_sections_for_version(session, pv.id):
        story.append(Paragraph(escape(section.heading or section.section_key or "Section"), styles["Heading1"]))
        for paragraph in (section.content or "").splitlines():
            story.append(Paragraph(escape(paragraph) or "&#160;", styles["Normal"]))
            story.append(Spacer(1, 6))
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build(story)
    data = buf.getvalue()
    _record_export(session, pv, proposal, data, "pdf")
    return data


def _record_export(session: Session, pv: ProposalVersion, proposal: Proposal | None, data: bytes, fmt: str) -> None:
    """Provenance: these exact bytes were rendered from this pinned version."""
    from govcon.submissions.manifest import record_proposal_artifact

    record_proposal_artifact(
        session,
        proposal_version_id=pv.id,
        opportunity_id=proposal.opportunity_id if proposal else None,
        content=data,
        fmt=fmt,
    )


def spreadsheet_text(value: Any) -> Any:
    """Prevent solicitation strings from becoming spreadsheet formulas."""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


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

    from govcon.proposals.versions import version_for_opportunity

    version_for_opportunity(session, opportunity_id, proposal_version_id)

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
            cell = ws.cell(row=row_idx, column=col, value=spreadsheet_text(value))
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
    include_proposal_pdf: bool = True,
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

    from govcon.compliance.deterministic import PackageFile, SubmissionPackage, required_files_present
    from govcon.submissions.manifest import current_package, verify_package
    from govcon.submissions.service import _extract_submission_info
    from govcon.compliance.matrix import active_requirements
    if _extract_submission_info(active_requirements(session, opportunity_id))["destination_conflicts"]:
        raise ValueError("submission_destination_conflict: resolve conflicting destinations before exporting")
    assembled = current_package(session, submission) if submission else None
    artifacts: dict[str, bytes] = {}
    if assembled:
        problems = verify_package(assembled)
        if problems:
            raise ValueError("; ".join(problems))
        for file in assembled.files:
            if not file.local_path:
                raise ValueError(f"required artifact has no local content: {file.name}")
            content = Path(file.local_path).read_bytes()
            if hashlib.sha256(content).hexdigest() != file.sha256:
                raise ValueError(f"assembled file changed: {file.name}")
            artifacts[file.name] = content
    if (include_proposal_docx or include_proposal_pdf or include_coverage_xlsx) and (not proposal or not proposal.current_version_id):
        raise ValueError("a current proposal version is required to generate the package")
    if proposal and proposal.current_version_id:
        pv = session.get(ProposalVersion, proposal.current_version_id)
        if pv is None:
            raise ValueError("current proposal version is unavailable")
        for enabled, name, generate in (
            (include_proposal_docx, f"proposal_v{pv.version_number}.docx", lambda: export_proposal_docx(session, proposal_version_id=pv.id)),
            (include_proposal_pdf, f"proposal_v{pv.version_number}.pdf", lambda: export_proposal_pdf(session, proposal_version_id=pv.id)),
            (include_coverage_xlsx, "coverage_matrix.xlsx", lambda: export_coverage_xlsx(session, opportunity_id=opportunity_id, proposal_version_id=pv.id)),
        ):
            if enabled and name.casefold() not in {n.casefold() for n in artifacts}:
                artifacts[name] = generate()
    required = submission.required_files if submission else None
    required = required.get("files", []) if isinstance(required, dict) else (required or [])
    export_files = [PackageFile(name=name, form_id=next((f.form_id for f in assembled.files if f.name == name), None) if assembled else None) for name in artifacts]
    if required:
        result = required_files_present(required, SubmissionPackage(files=export_files))
        if result.status != "pass":
            raise ValueError(f"required artifacts could not be assembled: {result.reason}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest: dict[str, Any] = {
            "opportunity_id": opportunity_id,
            "solicitation_number": sol_num,
            "files": [],
        }

        # Submission instructions
        instructions = _build_instructions_text(opp, submission)

        # Checklist
        from govcon.submissions.checklist import generate_final_checklist
        checklist = generate_final_checklist(session, opportunity_id=opportunity_id)
        for name in ("submission_instructions.txt", "submission_checklist.json", "file_manifest.json"):
            if name.casefold() in {n.casefold() for n in artifacts}:
                raise ValueError(f"assembled filename is reserved: {name}")
        artifacts["submission_instructions.txt"] = instructions.encode("utf-8")
        artifacts["submission_checklist.json"] = json.dumps(checklist, indent=2, default=str).encode("utf-8")
        for name, content in artifacts.items():
            zf.writestr(name, content)
            manifest["files"].append({"filename": name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})

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
