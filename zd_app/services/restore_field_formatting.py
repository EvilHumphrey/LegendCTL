"""Compact rendering for restore-point preview / result diff lines.

The pre-restore preview modal and the post-restore result page both surface
"before → after" or "expected → observed" diffs for fields the user may not
recognize at a glance. ``str()`` on a dataclass produces a long, class-name-
leaking form (``VibrationSettings(left_grip_strength=10, ...,
mode=<TriggerVibrationMode.NATIVE: 0>)``) that wraps across the modal and
is hostile to read.

:func:`format_field_value` returns a compact one-line key=value-style render
for the known restore-point payload dataclasses, and falls back to
``str(value)`` for scalars and enums (the convention the restore-result-enrichment
and pre-restore-preview work chose for the inline diff).

The helper is used in BOTH surfaces — :mod:`zd_app.services.restore_point_service`
(preview path + result-outcome path) — so the string the user sees in the
CONFIRM modal matches the string the user sees on the result page for the
same field-value pair.
"""

from __future__ import annotations

from typing import Any, Sequence

from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ControllerButtonTarget,
    LightingSettings,
    MotionSettings,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerSettings,
    VibrationSettings,
)


def format_field_value(name: str, value: Any) -> str:
    """Render ``value`` as a compact one-line string for restore-diff display.

    Dispatches on ``value``'s concrete type for the known controller-payload
    dataclasses (:class:`VibrationSettings`, :class:`StickDeadzones`,
    :class:`TriggerSettings`, :class:`AxisInversion`, :class:`ButtonMapping`,
    :class:`BackPaddleBinding`, :class:`LightingSettings`,
    :class:`MotionSettings`, :class:`RgbColor`) plus the
    ``tuple[SensitivityAnchor, ...]`` shape used by sensitivity curves.
    Falls back to ``str(value)`` for scalars and enums so the existing
    restore-result-enrichment convention is preserved unchanged.

    ``name`` is the snapshot field name (e.g. ``"sensitivity_left"``,
    ``"button_bindings[A]"``). It's accepted so callers can pass the
    delta's field-name unchanged and so future field-name-aware compaction
    has a place to land without re-threading call sites; today's dispatch
    is type-based.
    """

    del name  # currently unused — see docstring
    if value is None:
        return "None"
    if isinstance(value, VibrationSettings):
        return _format_vibration(value)
    if isinstance(value, StickDeadzones):
        return _format_deadzones(value)
    if isinstance(value, AxisInversion):
        return _format_axis_inversion(value)
    if isinstance(value, TriggerSettings):
        return _format_trigger(value)
    if isinstance(value, ButtonMapping):
        return _format_button_mapping(value)
    if isinstance(value, BackPaddleBinding):
        return _format_back_paddle(value)
    if isinstance(value, LightingSettings):
        return _format_lighting(value)
    if isinstance(value, MotionSettings):
        return _format_motion(value)
    if isinstance(value, RgbColor):
        return _format_rgb(value)
    if (
        isinstance(value, tuple)
        and value
        and all(isinstance(item, SensitivityAnchor) for item in value)
    ):
        return _format_sensitivity_curve(value)
    return str(value)


def _format_vibration(value: VibrationSettings) -> str:
    return (
        f"L={value.left_grip_strength} "
        f"R={value.right_grip_strength} "
        f"TL={value.left_trigger_motor_strength} "
        f"TR={value.right_trigger_motor_strength} "
        f"({value.mode.name})"
    )


def _format_deadzones(value: StickDeadzones) -> str:
    return (
        f"L_c/L_o/R_c/R_o = "
        f"{value.left_center}/{value.left_outer}/"
        f"{value.right_center}/{value.right_outer}"
    )


def _format_axis_inversion(value: AxisInversion) -> str:
    return f"x={value.x_inverted} y={value.y_inverted}"


def _format_trigger(value: TriggerSettings) -> str:
    return f"min={value.range_min} max={value.range_max} {value.mode.name}"


def _format_sensitivity_curve(value: Sequence[SensitivityAnchor]) -> str:
    return " ".join(f"({a.x},{a.y})" for a in value)


def _format_button_mapping(value: ButtonMapping) -> str:
    if value.target_kind == 0x01 and value.target_low == 0x00:
        try:
            return f"→{ControllerButtonTarget(value.target_value).name}"
        except ValueError:
            return f"kind=0x01 val=0x{value.target_value:02x}"
    return (
        f"kind=0x{value.target_kind:02x} "
        f"low=0x{value.target_low:02x} "
        f"val=0x{value.target_value:02x}"
    )


def _format_back_paddle(value: BackPaddleBinding) -> str:
    if value.target is None:
        return "unbound"
    return f"→{value.target.name}"


def _format_lighting(value: LightingSettings) -> str:
    state = "on" if value.light_on else "off"
    return (
        f"{state}, {value.mode.name}, "
        f"brightness={value.brightness_byte}, "
        f"{_format_rgb(value.color)}"
    )


def _format_motion(value: MotionSettings) -> str:
    return (
        f"target={value.target.name} mode={value.mode.name} "
        f"trigger_key=0x{value.trigger_key:02x} "
        f"sens={value.sensitivity}"
    )


def _format_rgb(value: RgbColor) -> str:
    return f"RGB({value.r},{value.g},{value.b})"


__all__ = ["format_field_value"]
