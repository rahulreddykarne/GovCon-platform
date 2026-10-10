"""Prompt registry: sync source-controlled prompts to the database and load active versions.

The application must never import a raw prompt constant from a business-service
module. Instead, ``prompt_registry.load("solicitation_analysis", version="active")``
returns the prompt body and metadata.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from govcon.models import PromptRegistryEntry
from govcon.prompting.loader import (
    PromptAsset,
    iter_markdown_prompts,
    load_markdown_prompt,
)

logger = logging.getLogger("govcon.prompting.registry")


class PromptRegistryAbsent(ValueError):
    """The prompt has never been synchronized to this registry."""


class PromptSetupError(RuntimeError):
    """Analysis cannot start because the prompt registry has no active version."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "Prompt registry is not ready. Missing active prompts: "
            + ", ".join(missing)
            + ". Run `govcon prompts sync` before analysis. "
            "If that command prints BLOCKED, the activation gate refused the version."
        )


# These must be active before a model analysis. Disk status is `active`.
REQUIRED_ANALYSIS_PROMPTS = ("solicitation_analysis",)


class PromptRegistryDenied(ValueError):
    """The registry knows the prompt but has no approved active version."""


class SyncReport(dict):
    """``{prompt_name: [synced versions]}`` plus what sync could not do.

    ``changed_in_place``: ``name@version`` whose file no longer matches the
    approved hash (left unchanged; loading it is refused). ``blocked``:
    ``name@version`` whose activation gate failed. ``activated``: versions
    activated through the gate during this sync.
    """

    def __init__(self) -> None:
        super().__init__()
        self.changed_in_place: list[str] = []
        self.blocked: list[str] = []
        self.activated: list[str] = []


def sync_prompts(
    session: Session,
    prompt_root: Path,
    *,
    settings=None,
    reapprove_changed: bool = False,
    actor_user_id: int | None = None,
) -> SyncReport:
    """Record source-controlled prompt versions in ``prompt_registry``.

    Versions are recorded inactive. A version marked ``status: active`` on
    disk is then activated only through ``activate_prompt`` (the same gate and
    audit event as ``govcon prompts activate``), and only when the prompt has
    no active version yet or the version is new in this sync, so an
    operator's activation or rollback is never undone by a sync.

    A version's content is immutable: a file edited in place is reported in
    ``changed_in_place`` and its approved hash is kept, so loading it is
    refused. ``reapprove_changed`` records the new content instead and, for an
    active version, re-runs the gate (a failed gate deactivates it).

    Returns a mapping of prompt names to their synced versions.
    """
    assets = iter_markdown_prompts(prompt_root)
    report = SyncReport()
    disk_active: list[tuple[PromptAsset, bool]] = []
    for asset in assets:
        if asset.metadata.get("status") == "placeholder":
            continue
        created, changed = _upsert_prompt(session, asset, reapprove_changed=reapprove_changed)
        report.setdefault(asset.name, []).append(asset.version)
        if changed:
            report.changed_in_place.append(f"{asset.name}@{asset.version}")
            if reapprove_changed and active_version(session, asset.name) == asset.version:
                _gated_activation(session, asset, prompt_root, settings, actor_user_id, report, reason="content_reapproved")
        if asset.metadata.get("status") == "active":
            disk_active.append((asset, created))
    session.flush()
    for asset, created in disk_active:
        current = active_version(session, asset.name)
        if current == asset.version or (current is not None and not created):
            continue
        if f"{asset.name}@{asset.version}" in report.blocked:
            continue
        _gated_activation(session, asset, prompt_root, settings, actor_user_id, report, reason="sync")
    session.flush()
    return report


def ensure_prompt_registry(session: Session, settings=None) -> list[str]:
    """Sync source prompts when a required analysis prompt is not active.

    Returns the required names that are still inactive after the sync.
    """
    from govcon.config import get_settings

    settings = settings or get_settings()
    missing = [name for name in REQUIRED_ANALYSIS_PROMPTS if active_version(session, name) is None]
    if not missing:
        return []
    sync_prompts(session, settings.resolved_prompt_root(), settings=settings)
    return [name for name in REQUIRED_ANALYSIS_PROMPTS if active_version(session, name) is None]


def require_prompt_registry(session: Session, settings=None) -> None:
    """Raise ``PromptSetupError`` when analysis prompts are still inactive."""
    missing = ensure_prompt_registry(session, settings)
    if missing:
        raise PromptSetupError(missing)


def _gated_activation(session: Session, asset: PromptAsset, prompt_root: Path, settings, actor_user_id, report: SyncReport, *, reason: str) -> None:
    try:
        activate_prompt(session, asset.name, asset.version, prompt_root=prompt_root, settings=settings, actor_user_id=actor_user_id, reason=reason)
        report.activated.append(f"{asset.name}@{asset.version}")
    except PromptActivationBlocked as exc:
        logger.warning("%s", exc)
        report.blocked.append(f"{asset.name}@{asset.version}")
        if active_version(session, asset.name) == asset.version:
            # Re-approval failed: the changed content must not stay active.
            _set_active(session, asset.name, None)


def _allowed_data_classes(asset: PromptAsset) -> list[str]:
    """Front-matter ``allowed_data_classes``; PUBLIC only when undeclared."""
    raw = asset.metadata.get("allowed_data_classes") or ""
    declared = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return declared or ["PUBLIC"]


def _upsert_prompt(session: Session, asset: PromptAsset, *, reapprove_changed: bool = False) -> tuple[bool, bool]:
    """Record one prompt version, inactive. Returns ``(created, changed_in_place)``.

    Never activates anything. An existing version keeps its approved
    ``prompt_hash`` unless ``reapprove_changed``.
    """
    existing = session.execute(
        select(PromptRegistryEntry).where(
            PromptRegistryEntry.prompt_name == asset.name,
            PromptRegistryEntry.prompt_version == asset.version,
        )
    ).scalar_one_or_none()
    changed = existing is not None and existing.prompt_hash != asset.content_hash
    values = {
        "prompt_name": asset.name,
        "prompt_version": asset.version,
        "task_type": asset.metadata.get("task_type", "unknown"),
        "provider_family": asset.metadata.get("provider_family"),
        "source_path": str(asset.path),
        "prompt_hash": asset.content_hash,
        "schema_version": asset.metadata.get("schema_version"),
        "default_settings": None,
        "allowed_data_classes": _allowed_data_classes(asset),
    }
    if existing is None:
        session.execute(
            pg_insert(PromptRegistryEntry)
            .values(**values, active=False)
            .on_conflict_do_nothing(constraint="uq_prompt_registry_name_version")
        )
        return True, False
    if changed and not reapprove_changed:
        logger.warning(
            "prompt %s@%s changed on disk since it was recorded; publish a new version or re-approve it",
            asset.name, asset.version,
        )
        return False, True
    for key, value in values.items():
        setattr(existing, key, value)
    return False, changed


def _set_active(session: Session, prompt_name: str, version: str | None) -> None:
    """Make exactly one version of a prompt active (none for ``None``).

    Internal: callers are ``activate_prompt`` and ``rollback_prompt``, which
    gate and audit the change.
    """
    session.execute(
        update(PromptRegistryEntry)
        .where(
            PromptRegistryEntry.prompt_name == prompt_name,
            PromptRegistryEntry.prompt_version != version if version is not None else PromptRegistryEntry.id.is_not(None),
        )
        .values(active=False)
    )
    if version is not None:
        session.execute(
            update(PromptRegistryEntry)
            .where(
                PromptRegistryEntry.prompt_name == prompt_name,
                PromptRegistryEntry.prompt_version == version,
            )
            .values(active=True)
        )
    session.flush()


class PromptActivationBlocked(RuntimeError):
    """A safety-critical prompt version failed its activation gate."""

    def __init__(self, prompt_name: str, version: str, failures: list[str]) -> None:
        self.prompt_name = prompt_name
        self.version = version
        self.failures = failures
        super().__init__(f"activation of {prompt_name}@{version} blocked: {'; '.join(failures)}")


def active_version(session: Session, prompt_name: str) -> str | None:
    return session.scalar(
        select(PromptRegistryEntry.prompt_version).where(
            PromptRegistryEntry.prompt_name == prompt_name,
            PromptRegistryEntry.active.is_(True),
        )
    )


def activate_prompt(
    session: Session,
    prompt_name: str,
    version: str,
    *,
    prompt_root: Path,
    settings=None,
    actor_user_id: int | None = None,
    reason: str = "manual",
) -> dict:
    """Gated activation (§43.8): the only way a version becomes active.

    The file must still hold the content recorded for the version, and
    safety-critical prompts must pass their gate. Records an audit event
    holding the previously active version so ``rollback_prompt`` can restore
    it. Historical ``ai_analyses`` keep the prompt hash they actually used.
    """
    from govcon.audit import record_audit
    from govcon.config import get_settings
    from govcon.prompting.evaluation import is_safety_critical, run_activation_gate

    settings = settings or get_settings()
    row = session.execute(
        select(PromptRegistryEntry).where(
            PromptRegistryEntry.prompt_name == prompt_name,
            PromptRegistryEntry.prompt_version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"prompt {prompt_name!r} version {version!r} not found; run `govcon prompts sync`")
    asset = _approved_asset(row, prompt_root)
    if asset is None:
        raise PromptActivationBlocked(
            prompt_name, version,
            ["prompt file is missing or no longer matches the recorded content; publish a new version or re-approve it"],
        )
    gate: dict = {"required": False, "passed": True, "checks": []}
    if is_safety_critical(prompt_name) and settings.prompt_enable_regression_gate:
        result = run_activation_gate(asset, prompt_root, settings=settings)
        gate = {"required": True, "passed": result.passed, "checks": result.checks}
        if not result.passed:
            raise PromptActivationBlocked(prompt_name, version, result.failures)
    previous = active_version(session, prompt_name)
    _set_active(session, prompt_name, version)
    record_audit(
        session,
        action_type="prompt_activated",
        user_id=actor_user_id,
        entity_type="prompt_registry",
        entity_id=row.id,
        old_value={"prompt_name": prompt_name, "active_version": previous},
        new_value={"prompt_name": prompt_name, "active_version": version, "prompt_hash": row.prompt_hash, "gate": gate, "reason": reason},
    )
    return {"prompt_name": prompt_name, "previous_version": previous, "active_version": version, "gate": gate}


def rollback_prompt(session: Session, prompt_name: str, *, actor_user_id: int | None = None) -> dict:
    """Restore the version that was active before the latest activation (§43.9)."""
    from sqlalchemy import desc

    from govcon.audit import record_audit
    from govcon.models import AuditEvent

    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.action_type == "prompt_activated")
        .order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
    ).all()
    for event in events:
        new_value = event.new_value or {}
        if new_value.get("prompt_name") != prompt_name:
            continue
        previous = (event.old_value or {}).get("active_version")
        if not previous:
            raise ValueError(f"no earlier active version recorded for {prompt_name!r}")
        current = active_version(session, prompt_name)
        _set_active(session, prompt_name, previous)
        record_audit(
            session,
            action_type="prompt_rolled_back",
            user_id=actor_user_id,
            entity_type="prompt_registry",
            old_value={"prompt_name": prompt_name, "active_version": current},
            new_value={"prompt_name": prompt_name, "active_version": previous},
        )
        return {"prompt_name": prompt_name, "rolled_back_from": current, "active_version": previous}
    raise ValueError(f"no activation history for {prompt_name!r}")


def load_prompt(
    session: Session,
    prompt_name: str,
    *,
    version: str = "active",
    prompt_root: Path | None = None,
) -> PromptAsset:
    """Load a prompt by name and version.

    ``version="active"`` returns the currently active version from the
    database, then reads the source file. An explicit version string
    loads that exact version.

    The file (and every shared include it renders) must still hold exactly
    the content recorded in the registry; an edited file is refused with
    ``PromptRegistryDenied`` rather than executed without approval.
    """
    if version == "active":
        row = session.execute(
            select(PromptRegistryEntry).where(
                PromptRegistryEntry.prompt_name == prompt_name,
                PromptRegistryEntry.active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            known = session.scalar(select(PromptRegistryEntry.id).where(
                PromptRegistryEntry.prompt_name == prompt_name
            ).limit(1))
            error = PromptRegistryDenied if known is not None else PromptRegistryAbsent
            raise error(
                f"no active version for prompt {prompt_name!r}. "
                "Run `govcon prompts sync` before analysis."
            )
    else:
        row = session.execute(
            select(PromptRegistryEntry).where(
                PromptRegistryEntry.prompt_name == prompt_name,
                PromptRegistryEntry.prompt_version == version,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"prompt {prompt_name!r} version {version!r} not found")

    asset = _approved_asset(row, prompt_root)
    if asset is None:
        raise PromptRegistryDenied(
            f"prompt {prompt_name!r} {row.prompt_version} is missing or no longer matches its approved content "
            f"(hash {(row.prompt_hash or '')[:12]}…); publish a new version or re-approve it"
        )
    if prompt_root is not None:
        _verify_includes(session, asset, prompt_root)
    return asset


def _source_path(row: PromptRegistryEntry, prompt_root: Path | None) -> Path:
    source_path = Path(row.source_path)
    if not source_path.is_absolute() and prompt_root:
        for candidate in (prompt_root / source_path.name, source_path):
            if candidate.exists():
                return candidate
    return source_path


def _approved_asset(row: PromptRegistryEntry, prompt_root: Path | None) -> PromptAsset | None:
    """The row's prompt file, only if its bytes still match the recorded hash."""
    try:
        asset = load_markdown_prompt(_source_path(row, prompt_root))
    except (OSError, ValueError):
        return None
    if not row.prompt_hash or asset.content_hash != row.prompt_hash:
        return None
    return asset


def _verify_includes(session: Session, asset: PromptAsset, prompt_root: Path) -> None:
    """Every shared include must match a recorded (synced) version of itself."""
    from govcon.prompting.renderer import _parse_includes, _resolve_include

    for name in _parse_includes(asset.metadata.get("includes", "")):
        fragment = _resolve_include(name, prompt_root)
        if fragment is None:
            raise PromptRegistryDenied(f"required shared include {name!r} is missing or cannot be loaded")
        recorded = session.scalar(
            select(PromptRegistryEntry.prompt_hash).where(
                PromptRegistryEntry.prompt_name == fragment.name,
                PromptRegistryEntry.prompt_version == fragment.version,
            )
        )
        if recorded != fragment.content_hash:
            raise PromptRegistryDenied(
                f"shared include {fragment.name}@{fragment.version} used by {asset.name!r} "
                "does not match its recorded content; run `govcon prompts sync` (or re-approve the change)"
            )


def load_prompt_from_disk(prompt_root: Path, prompt_name: str) -> PromptAsset:
    """Load a prompt directly from disk without database access.

    Searches the prompt root recursively for a file whose front-matter
    ``name`` matches and whose ``status`` is ``active``.
    """
    for asset in iter_markdown_prompts(prompt_root):
        if asset.name == prompt_name and asset.metadata.get("status") == "active":
            return asset
    raise ValueError(f"no active prompt file for {prompt_name!r} in {prompt_root}")
