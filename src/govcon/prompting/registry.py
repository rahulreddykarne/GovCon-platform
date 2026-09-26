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
from govcon.prompting.loader import PromptAsset, iter_markdown_prompts, load_markdown_prompt

logger = logging.getLogger("govcon.prompting.registry")


def sync_prompts(session: Session, prompt_root: Path) -> dict[str, list[str]]:
    """Scan source-controlled prompts and upsert them into ``prompt_registry``.

    Returns a mapping of prompt names to their synced versions.
    """
    assets = iter_markdown_prompts(prompt_root)
    synced: dict[str, list[str]] = {}
    for asset in assets:
        if asset.metadata.get("status") == "placeholder":
            continue
        _upsert_prompt(session, asset)
        synced.setdefault(asset.name, []).append(asset.version)
    session.flush()
    return synced


def _upsert_prompt(session: Session, asset: PromptAsset) -> None:
    """Insert or update a single prompt registry entry."""
    values = {
        "prompt_name": asset.name,
        "prompt_version": asset.version,
        "task_type": asset.metadata.get("task_type", "unknown"),
        "provider_family": asset.metadata.get("provider_family"),
        "source_path": str(asset.path),
        "prompt_hash": asset.content_hash,
        "schema_version": asset.metadata.get("schema_version"),
        "default_settings": None,
        "allowed_data_classes": None,
    }
    is_active = asset.metadata.get("status") == "active"
    stmt = (
        pg_insert(PromptRegistryEntry)
        .values(**values, active=is_active)
        .on_conflict_do_update(
            constraint="uq_prompt_registry_name_version",
            set_={
                "prompt_hash": values["prompt_hash"],
                "source_path": values["source_path"],
                "schema_version": values["schema_version"],
                "task_type": values["task_type"],
                "provider_family": values["provider_family"],
            },
        )
    )
    session.execute(stmt)
    if is_active:
        activate_version(session, asset.name, asset.version)


def activate_version(session: Session, prompt_name: str, version: str) -> None:
    """Make exactly one version of a prompt active."""
    session.execute(
        update(PromptRegistryEntry)
        .where(
            PromptRegistryEntry.prompt_name == prompt_name,
            PromptRegistryEntry.prompt_version != version,
        )
        .values(active=False)
    )
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
) -> dict:
    """Gated activation (§43.8): safety-critical prompts must pass their gate.

    Records an audit event holding the previously active version so
    ``rollback_prompt`` can restore it. Historical ``ai_analyses`` keep the
    prompt hash they actually used.
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
    gate: dict = {"required": False, "passed": True, "checks": []}
    if is_safety_critical(prompt_name) and settings.prompt_enable_regression_gate:
        asset = load_markdown_prompt(Path(row.source_path))
        result = run_activation_gate(asset, prompt_root, settings=settings)
        gate = {"required": True, "passed": result.passed, "checks": result.checks}
        if not result.passed:
            raise PromptActivationBlocked(prompt_name, version, result.failures)
    previous = active_version(session, prompt_name)
    activate_version(session, prompt_name, version)
    record_audit(
        session,
        action_type="prompt_activated",
        user_id=actor_user_id,
        entity_type="prompt_registry",
        entity_id=row.id,
        old_value={"prompt_name": prompt_name, "active_version": previous},
        new_value={"prompt_name": prompt_name, "active_version": version, "prompt_hash": row.prompt_hash, "gate": gate},
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
        activate_version(session, prompt_name, previous)
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
    """
    if version == "active":
        row = session.execute(
            select(PromptRegistryEntry).where(
                PromptRegistryEntry.prompt_name == prompt_name,
                PromptRegistryEntry.active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"no active version for prompt {prompt_name!r}")
        source_path = Path(row.source_path)
    else:
        row = session.execute(
            select(PromptRegistryEntry).where(
                PromptRegistryEntry.prompt_name == prompt_name,
                PromptRegistryEntry.prompt_version == version,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"prompt {prompt_name!r} version {version!r} not found")
        source_path = Path(row.source_path)

    if not source_path.is_absolute() and prompt_root:
        for candidate in (prompt_root / source_path.name, source_path):
            if candidate.exists():
                source_path = candidate
                break

    return load_markdown_prompt(source_path)


def load_prompt_from_disk(prompt_root: Path, prompt_name: str) -> PromptAsset:
    """Load a prompt directly from disk without database access.

    Searches the prompt root recursively for a file whose front-matter
    ``name`` matches and whose ``status`` is ``active``.
    """
    for asset in iter_markdown_prompts(prompt_root):
        if asset.name == prompt_name and asset.metadata.get("status") == "active":
            return asset
    raise ValueError(f"no active prompt file for {prompt_name!r} in {prompt_root}")
