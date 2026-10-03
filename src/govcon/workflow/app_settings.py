"""Owner-editable workflow settings, stored in ``app_settings`` (ADR-067).

These are the choices the owner adjusts from the web Settings page as
circumstances change, as opposed to deployment configuration in ``.env``.
Every change is validated and audited.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.models import AppSetting, User

REVIEWER_ASSIGNMENT = "reviewer_assignment"
AUTO_PREPARE = "auto_prepare_on_pursuit"
AUTO_PURSUE = "auto_pursue"
DEADLINE_EXCEPTION = "single_reviewer_deadline_exception"
REVIEWER_MODES = ("manual", "named", "all_active")

DEFAULTS: dict[str, dict[str, Any]] = {
    # Who is assigned when automatic preparation sets up the review.
    REVIEWER_ASSIGNMENT: {"mode": "manual", "user_ids": []},
    # Whether creating a pursuit queues document processing through the decision package.
    AUTO_PREPARE: {"enabled": True},
    # Automatic pursuit of top-ranked rule matches (roadmap Q4 = B, ADR-069).
    AUTO_PURSUE: {"enabled": True, "min_score": 75, "min_days": 10, "max_per_day": 3},
    # One completed review may approve when the deadline is close (roadmap Q5, ADR-070).
    DEADLINE_EXCEPTION: {"enabled": True},
}


class SettingError(ValueError):
    pass


def get_setting(session: Session, key: str) -> dict[str, Any]:
    row = session.get(AppSetting, key)
    return {**DEFAULTS.get(key, {}), **(row.value if row is not None else {})}


def _validate(session: Session, key: str, value: dict[str, Any]) -> dict[str, Any]:
    if key == REVIEWER_ASSIGNMENT:
        mode = value.get("mode")
        if mode not in REVIEWER_MODES:
            raise SettingError(f"reviewer assignment mode must be one of {', '.join(REVIEWER_MODES)}")
        user_ids = sorted({int(u) for u in value.get("user_ids") or []})
        if mode == "named":
            if not user_ids:
                raise SettingError("choose at least one reviewer for named assignment")
            active = set(session.scalars(select(User.id).where(User.id.in_(user_ids), User.is_active.is_(True),
                                                               User.role.in_(("owner", "approver", "reviewer")))))
            missing = [u for u in user_ids if u not in active]
            if missing:
                raise SettingError(f"users {missing} are not active reviewers")
        return {"mode": mode, "user_ids": user_ids if mode == "named" else []}
    if key in (AUTO_PREPARE, DEADLINE_EXCEPTION):
        return {"enabled": bool(value.get("enabled"))}
    if key == AUTO_PURSUE:
        try:
            clean = {
                "enabled": bool(value.get("enabled")),
                "min_score": float(value.get("min_score", 75)),
                "min_days": int(value.get("min_days", 10)),
                "max_per_day": int(value.get("max_per_day", 3)),
            }
        except (TypeError, ValueError) as exc:
            raise SettingError("auto-pursue thresholds must be numbers") from exc
        if not 0 <= clean["min_score"] <= 100:
            raise SettingError("the minimum score must be between 0 and 100")
        if clean["min_days"] < 0 or not 0 <= clean["max_per_day"] <= 50:
            raise SettingError("minimum days must be 0 or more and the daily maximum between 0 and 50")
        return clean
    raise SettingError(f"unknown setting {key!r}")


def set_setting(session: Session, key: str, value: dict[str, Any], *, actor: User) -> dict[str, Any]:
    """Validate, store and audit a setting change (owner only)."""
    require_permission(actor, "manage_users")
    clean = _validate(session, key, value)
    row = session.get(AppSetting, key, with_for_update=True)
    old = get_setting(session, key)
    if row is None:
        row = AppSetting(key=key, value=clean, updated_by_user_id=actor.id)
        session.add(row)
    else:
        row.value = clean
        row.updated_by_user_id = actor.id
    session.flush()
    record_audit(session, action_type="app_setting_changed", user_id=actor.id, entity_type="app_settings",
                 old_value={"key": key, "value": old}, new_value={"key": key, "value": clean})
    return clean
