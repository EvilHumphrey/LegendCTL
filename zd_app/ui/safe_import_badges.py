"""Safety badges for profile cards + import results (no toggle).

Truthful, descriptive badges only — no "tournament/ban/anti-cheat" wording
anywhere (per the no-claims policy). ``No Automation`` is the default badge for every wrapper
profile because the data model has no automation-capable fields; the other
badges describe facts about an import. Colors follow the spec: green = safe,
yellow = review/device, grey = blocked.
"""

from __future__ import annotations

from enum import Enum

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.ui.safe_import_model import ImportResult, has_device_settings
from zd_app.ui.themes import COLORS


class BadgeKind(Enum):
    NO_AUTOMATION = "no_automation"
    AUTOMATION_BLOCKED = "automation_blocked"
    DEVICE_SETTINGS = "device_settings"
    NEEDS_REVIEW = "needs_review"


# Badge color by semantic group (spec §Badges: green safe, yellow review/device,
# grey blocked).
_BADGE_COLOR = {
    BadgeKind.NO_AUTOMATION: COLORS["success"],
    BadgeKind.AUTOMATION_BLOCKED: COLORS["text.muted"],
    BadgeKind.DEVICE_SETTINGS: COLORS["warning"],
    BadgeKind.NEEDS_REVIEW: COLORS["warning"],
}


def badge_label(kind: BadgeKind) -> str:
    return t(f"badge.{kind.value}.label")


def badge_tooltip(kind: BadgeKind) -> str:
    return t(f"badge.{kind.value}.tooltip")


def badges_for_profile(profile) -> list[BadgeKind]:
    """Badges for a stored wrapper profile card.

    Wrapper profiles never contain automation-capable fields, so every profile
    is truthfully ``No Automation``. Since Device overrides were added, Device settings (polling rate,
    step-size) are an opt-in override rather than always present, so the Device
    badge is meaningful: it flags profiles that carry a Device override.
    """

    badges = [BadgeKind.NO_AUTOMATION]
    if has_device_settings(getattr(profile, "snapshot", None)):
        badges.append(BadgeKind.DEVICE_SETTINGS)
    return badges


def badges_for_import_result(result: ImportResult) -> list[BadgeKind]:
    """Badges describing an import outcome (result screen + scan summary)."""

    badges: list[BadgeKind] = []
    if result.has_automation:
        badges.append(BadgeKind.AUTOMATION_BLOCKED)
    # The saved profile is always automation-free (blocked + discarded).
    if result.ok:
        badges.append(BadgeKind.NO_AUTOMATION)
    if result.has_device_changes:
        badges.append(BadgeKind.DEVICE_SETTINGS)
    if result.unknown_fields:
        badges.append(BadgeKind.NEEDS_REVIEW)
    return badges


def render_badges(
    badges: list[BadgeKind],
    *,
    parent: int | str | None = None,
    tag_prefix: str | None = None,
    horizontal: bool = True,
) -> None:
    """Render a row of badge chips with explanatory tooltips."""

    if not badges:
        return
    group_kwargs: dict = {"horizontal": horizontal}
    if parent is not None:
        group_kwargs["parent"] = parent
    with dpg.group(**group_kwargs):
        for kind in badges:
            text_kwargs: dict = {
                "default_value": f"[ {badge_label(kind)} ]",
                "color": _BADGE_COLOR[kind],
            }
            if tag_prefix is not None:
                text_kwargs["tag"] = f"{tag_prefix}_badge_{kind.value}"
            chip = dpg.add_text(**text_kwargs)
            with dpg.tooltip(chip):
                dpg.add_text(badge_tooltip(kind), wrap=320)
