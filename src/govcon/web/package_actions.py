"""Browser downloads and bounded uploads through the shared package gates."""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from govcon.config import get_settings
from govcon.db import session_scope
from govcon.models import Submission, User
from govcon.web.routes import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _form_int,
    _NeedsLogin,
    _redirect,
    _require_login,
)
from govcon.web.security import PACKAGE_UPLOAD_LIMIT
from govcon.web.workspace_actions import checked_proposal
from govcon.workflow.invalidation import lock_one

UPLOAD_LIMIT = PACKAGE_UPLOAD_LIMIT
FILE_LIMIT = 20_000_000


async def workspace_package_action(request: Request, opp_id: int, action: str) -> Response:
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return _redirect("/login", request=request)
    form = await request.form()
    uploads: list[tuple[str, bytes]] = []
    try:
        if action == "assemble":
            files = form.getlist("files")
            if not files or len(files) > 20:
                raise ValueError("upload between 1 and 20 package files")
            for upload in files:
                if not isinstance(upload, UploadFile):
                    raise ValueError("choose files to upload")
                content = await upload.read(FILE_LIMIT + 1)
                if not content or len(content) > FILE_LIMIT:
                    raise ValueError("each file must contain data and be at most 20 MB")
                uploads.append((upload.filename or "", content))
            if sum(len(content) for _, content in uploads) > UPLOAD_LIMIT:
                raise ValueError("the package exceeds the 50 MB upload limit")
        return await run_in_threadpool(_package_action, request, opp_id, user, action, form, uploads)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(f"/workspace/{opp_id}?tab=submission", error=_error_text(exc), request=request)
    except OSError:
        return _redirect(f"/workspace/{opp_id}?tab=submission", error="Package files could not be read or stored. Check storage and retry.", request=request)
    finally:
        await form.close()


def _submission(db: Session, opportunity_id: int, expected: int | None) -> Submission:
    row = lock_one(db, select(Submission).where(Submission.opportunity_id == opportunity_id))
    if row is None:
        raise ValueError("generate submission instructions first")
    if expected is None or row.version != expected:
        raise ValueError("the submission changed since this page loaded; reload before trying again")
    if row.status in {"submitted", "confirmed", "withdrawn"}:
        raise ValueError("the submission is closed")
    return row


def _pricing_rows(raw: str) -> list[dict[str, Any]]:
    rows = []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or not any(field.strip() for field in fields):
            continue
        if len(fields) != 3 or not fields[0].strip():
            raise ValueError("pricing rows must contain CLIN, quantity, unit price")
        try:
            quantity, price = (Decimal(field.strip()) for field in fields[1:])
        except InvalidOperation as exc:
            raise ValueError("pricing quantities and prices must be numbers") from exc
        if not quantity.is_finite() or not price.is_finite() or quantity < 0 or price < 0:
            raise ValueError("pricing quantities and prices must be finite and nonnegative")
        rows.append({"clin": fields[0].strip(), "quantity": str(quantity), "unit_price": str(price)})
    return rows


def _uploaded_package(form: Any, uploads: list[tuple[str, bytes]], directory: Path, submission: Submission, version_id: int):
    from govcon.compliance.deterministic import PackageFile, SubmissionPackage

    names = [name for name, _ in uploads]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("package filenames must be unique")
    for name in names:
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if (not name or name in {".", ".."} or any(char in name for char in '/\\:<>"|?*')
                or name.split(".", 1)[0].upper() in reserved or len(name.encode("utf-8")) > 240
                or any(ord(char) < 32 for char in name) or name.endswith((".", " "))):
            raise ValueError("package filenames must be plain filenames without paths")
    proposal_name = str(form.get("proposal_filename") or "").strip()
    if proposal_name not in names:
        raise ValueError("name the uploaded proposal file")
    pricing_names = {name.strip() for name in str(form.get("pricing_filenames") or "").split(",") if name.strip()}
    signed_names = {name.strip() for name in str(form.get("signed_filenames") or "").split(",") if name.strip()}
    if not (pricing_names | signed_names) <= set(names) or proposal_name in pricing_names:
        raise ValueError("pricing and signed filenames must match uploaded files; the proposal cannot be a pricing file")
    metadata: dict[str, tuple[str | None, int | None]] = {}
    for fields in csv.reader(io.StringIO(str(form.get("file_details") or ""))):
        if not fields or not any(field.strip() for field in fields):
            continue
        if len(fields) != 3 or fields[0].strip() not in names or fields[0].strip() in metadata:
            raise ValueError("file details must contain uploaded filename, form ID, page count once per file")
        pages = fields[2].strip()
        if pages and (not pages.isascii() or not pages.isdigit() or int(pages) < 1):
            raise ValueError("page counts must be positive whole numbers")
        metadata[fields[0].strip()] = (fields[1].strip() or None, int(pages) if pages else None)
    package = SubmissionPackage(
        proposal_version_id=version_id, submission_method=submission.submission_method,
        recipient_email=submission.recipient_email,
        portal=submission.portal_url or submission.portal_name or submission.submission_destination,
        amendments_acknowledged=[item.strip() for item in str(form.get("amendments_acknowledged") or "").split(",") if item.strip()],
        pricing_rows=_pricing_rows(str(form.get("pricing_rows") or "")),
        representations_complete=form.get("representations_complete") == "yes",
        certifications_complete=form.get("certifications_complete") == "yes",
    )
    directory.mkdir(parents=True)
    for name, content in uploads:
        path = directory / name
        path.write_bytes(content)
        form_id, page_count = metadata.get(name, (None, None))
        package.files.append(PackageFile(
            name=name, role="proposal" if name == proposal_name else ("pricing" if name in pricing_names else "other"),
            signed=name in signed_names, form_id=form_id, page_count=page_count, local_path=str(path),
        ))
    return package


def _package_action(request: Request, opp_id: int, user: User, action: str, form: Any, uploads: list[tuple[str, bytes]]) -> Response:
    from govcon.proposals.export import (
        export_coverage_xlsx,
        export_proposal_docx,
        export_proposal_pdf,
        export_submission_zip,
    )
    from govcon.submissions.manifest import (
        approve_proposal_artifact,
        assemble_package,
        current_package,
    )

    directory: Path | None = None
    target = f"/workspace/{opp_id}?tab=submission"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review" if action == "export" else "approve")
            proposal = checked_proposal(db, opp_id, _form_int(form.get("expected_proposal_version")))
            version_id = proposal.current_version_id
            assert version_id is not None
            if action == "export":
                fmt = str(form.get("format") or "")
                if fmt == "docx":
                    data, mime = export_proposal_docx(db, proposal_version_id=version_id), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                elif fmt == "pdf":
                    data, mime = export_proposal_pdf(db, proposal_version_id=version_id), "application/pdf"
                elif fmt == "xlsx":
                    data, mime = export_coverage_xlsx(db, opportunity_id=opp_id, proposal_version_id=version_id), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                elif fmt == "zip":
                    submission = _submission(db, opp_id, _form_int(form.get("expected_submission_version")))
                    if current_package(db, submission) is None:
                        raise ValueError("assemble your package before downloading it")
                    data = export_submission_zip(db, opportunity_id=opp_id, include_proposal_docx=False,
                                                 include_proposal_pdf=False, include_coverage_xlsx=False)
                    mime = "application/zip"
                else:
                    raise ValueError("choose DOCX, PDF, coverage XLSX, or the assembled ZIP")
                response = Response(data, media_type=mime, headers={
                    "Content-Disposition": f'attachment; filename="opportunity_{opp_id}_version_{version_id}.{fmt}"',
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                })
            elif action == "assemble":
                submission = _submission(db, opp_id, _form_int(form.get("expected_submission_version")))
                root = (get_settings().data_dir / "submission-packages" / str(opp_id)).resolve()
                directory = root / uuid4().hex
                package = _uploaded_package(form, uploads, directory, submission, version_id)
                reason = str(form.get("content_review_reason") or "").strip()
                if reason:
                    proposal_file = next(file for file in package.files if file.role == "proposal")
                    assert proposal_file.local_path is not None
                    approve_proposal_artifact(db, opportunity_id=opp_id, proposal_version_id=version_id,
                                              local_path=proposal_file.local_path, actor=actor, reason=reason)
                assemble_package(db, opportunity_id=opp_id, package=package, actor=actor)
                response = _redirect(target, notice="Package assembled. Run preflight on these exact files before approval.", request=request)
            elif action == "preflight":
                from govcon.compliance.submission_preflight import (
                    run_submission_preflight,
                )

                submission = _submission(db, opp_id, _form_int(form.get("expected_submission_version")))
                package = current_package(db, submission)
                if package is None or package.proposal_version_id != version_id:
                    raise ValueError("assemble a package for the current proposal version first")
                result = run_submission_preflight(db, opp_id, package, use_ai=False)
                response = _redirect(target, notice="Preflight passed. Final proposal approval is still required." if result["ready"]
                                     else "Preflight found blockers. Review the checklist before approval.", request=request)
            else:
                raise ValueError("unknown package action")
        return response
    except Exception:
        # Only files written into this request's own UUID directory are removed.
        # Historical and current packages use different directories and remain intact.
        if directory is not None and directory.is_dir():
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()
        raise
