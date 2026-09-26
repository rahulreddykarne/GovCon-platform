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
