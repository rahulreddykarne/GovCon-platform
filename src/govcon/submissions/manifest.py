"""Content-addressed, append-only snapshots of assembled submission packages.

A file with role ``proposal`` must be bound to the proposal version the
package names (which must belong to the opportunity): its exact bytes were
either exported from that pinned version (``proposal_artifact_exported``) or
approved in a content review of those bytes (``proposal_artifact_approved``,
for a signed or edited final file). Both are append-only audit records.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.compliance.deterministic import SubmissionPackage
from govcon.models import PackageManifest, Submission, User
from govcon.workflow.invalidation import lock_one, lock_opportunity


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def verify_package(package: SubmissionPackage) -> list[str]:
    """Problems that stop ``package`` from counting as an assembled package.

    Every file must have retrievable content (``local_path``) whose bytes match
    the recorded SHA-256 and size. A manifest that only describes files (a
    planning list) is not an assembled package and never passes.
    """
    problems = []
    names: set[str] = set()
    for file in package.files:
        if not isinstance(file.name, str):
            problems.append("package filenames must be strings")
            continue
        if not file.name or "/" in file.name or "\\" in file.name or file.name in {".", ".."} or ":" in file.name:
            problems.append(f"unsafe package filename: {file.name}")
        if file.name.casefold() in names:
            problems.append(f"duplicate package filename: {file.name}")
        names.add(file.name.casefold())
        if not isinstance(file.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", file.sha256):
            problems.append(f"content hash is missing or invalid: {file.name}")
        if type(file.size_bytes) is not int or file.size_bytes < 0:
            problems.append(f"file size is missing or invalid: {file.name}")
        if not file.local_path:
            problems.append(f"assembled file has no retrievable content: {file.name}")
            continue
        try:
            content = Path(file.local_path).read_bytes()
            if hashlib.sha256(content).hexdigest() != file.sha256 or len(content) != file.size_bytes:
                problems.append(f"assembled file changed: {file.name}")
        except (OSError, TypeError, ValueError):
            problems.append(f"assembled file is unavailable: {file.name}")
    return problems


ARTIFACT_EXPORTED = "proposal_artifact_exported"
ARTIFACT_APPROVED = "proposal_artifact_approved"


def record_proposal_artifact(session: Session, *, proposal_version_id: int, opportunity_id: int | None, content: bytes, fmt: str, actor_id: int | None = None) -> str:
    """Record that ``content`` was exported from ``proposal_version_id``. Returns its SHA-256."""
    digest = hashlib.sha256(content).hexdigest()
    record_audit(
        session,
        action_type=ARTIFACT_EXPORTED,
        user_id=actor_id,
        opportunity_id=opportunity_id,
        entity_type="proposal_versions",
        entity_id=proposal_version_id,
        new_value={"proposal_version_id": proposal_version_id, "sha256": digest, "size_bytes": len(content), "format": fmt},
    )
    return digest


def approve_proposal_artifact(
    session: Session, *, opportunity_id: int, proposal_version_id: int, local_path: str, actor: User, reason: str
) -> str:
    """Content-review approval of a signed or edited final proposal file's exact bytes.

    The approver attests that these bytes carry the content of the pinned
    proposal version. Returns the approved SHA-256.
    """
    from govcon.proposals.versions import version_for_opportunity

    require_permission(actor, "approve")
    if not reason or len(reason.strip()) < 10:
        raise ValueError("a content-review reason (10+ characters) is required to approve a proposal file")
    version_for_opportunity(session, opportunity_id, proposal_version_id)
    content = Path(local_path).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    record_audit(
        session,
        action_type=ARTIFACT_APPROVED,
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="proposal_versions",
        entity_id=proposal_version_id,
        new_value={"proposal_version_id": proposal_version_id, "sha256": digest, "size_bytes": len(content), "filename": Path(local_path).name, "reason": reason.strip()},
    )
    return digest


def proposal_artifact_problems(session: Session, opportunity_id: int, package: SubmissionPackage) -> list[str]:
    """Why the package's proposal files are not bound to its proposal version (empty when bound)."""
    from govcon.models import AuditEvent
    from govcon.proposals.versions import version_for_opportunity

    proposal_files = [f for f in package.files if f.role == "proposal"]
    if package.proposal_version_id is None:
        return ["the package names no proposal version for its proposal file(s)"] if proposal_files else []
    try:
        version_for_opportunity(session, opportunity_id, int(package.proposal_version_id))
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    if not proposal_files:
        return []
    known = {
        (event.new_value or {}).get("sha256")
        for event in session.scalars(
            select(AuditEvent).where(
                AuditEvent.action_type.in_([ARTIFACT_EXPORTED, ARTIFACT_APPROVED]),
                AuditEvent.entity_type == "proposal_versions",
                AuditEvent.entity_id == int(package.proposal_version_id),
            )
        ).all()
    }
    return [
        f"proposal file {f.name} is not an export of proposal version {package.proposal_version_id} "
        "and has no content-review approval of these exact bytes"
        for f in proposal_files
        if not f.sha256 or f.sha256 not in known
    ]


def snapshot_package(session: Session, submission: Submission, package: SubmissionPackage, *, actor: User | None = None) -> PackageManifest:
    submission = lock_one(session, select(Submission).where(Submission.id == submission.id))
    manifest = json.loads(json.dumps(package.manifest(), default=str))
    digest = manifest_hash(manifest)
    row = session.scalar(select(PackageManifest).where(PackageManifest.submission_id == submission.id, PackageManifest.sha256 == digest))
    if row is None:
        row = PackageManifest(submission_id=submission.id, sha256=digest, manifest=manifest, actor_id=actor.id if actor else None)
        session.add(row)
        session.flush()
    if submission.package_manifest_hash != digest:
        submission.readiness_status = "not_ready"
        submission.package_manifest_hash = digest
        submission.version = (submission.version or 1) + 1
    submission.assembled_files = {"files": manifest["files"]}
    submission.completed_actions = {"amendment_acknowledgments": manifest["amendments_acknowledged"] or [], "representations_complete": manifest["representations_complete"], "certifications_complete": manifest["certifications_complete"]}
    return row


def assemble_package(session: Session, *, opportunity_id: int, package: SubmissionPackage, actor: User) -> PackageManifest:
    require_permission(actor, "approve")
    lock_opportunity(session, opportunity_id)
    submission = lock_one(session, select(Submission).where(Submission.opportunity_id == opportunity_id))
    if submission is None:
        raise ValueError("generate submission instructions before assembling the package")
    if submission.status in {"submitted", "confirmed", "withdrawn"}:
        raise ValueError("a submitted or withdrawn package cannot be changed")
    for file in package.files:
        if not file.local_path:
            raise ValueError(f"local_path is required to assemble {file.name}")
        content = Path(file.local_path).read_bytes()
        file.sha256 = hashlib.sha256(content).hexdigest()
        file.size_bytes = len(content)
    problems = verify_package(package) + proposal_artifact_problems(session, opportunity_id, package)
    if problems:
        raise ValueError("; ".join(problems))
    row = snapshot_package(session, submission, package, actor=actor)
    record_audit(session, user_id=actor.id, opportunity_id=opportunity_id, action_type="submission_package_assembled", entity_type="submission", entity_id=submission.id, new_value={"manifest_sha256": row.sha256})
    return row


def current_package(session: Session, submission: Submission) -> SubmissionPackage | None:
    if not submission.package_manifest_hash:
        return None
    row = session.scalar(select(PackageManifest).where(PackageManifest.submission_id == submission.id, PackageManifest.sha256 == submission.package_manifest_hash))
    if row is None or manifest_hash(row.manifest) != row.sha256:
        return None
    return SubmissionPackage.from_dict(row.manifest)
