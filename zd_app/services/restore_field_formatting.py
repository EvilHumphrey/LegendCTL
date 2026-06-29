"""Compact rendering for restore-point preview / result diff lines.

The pre-restore preview modal and the post-restore result page both surface
"before → after" or "expected → observed" diffs for fields the user may not
recognize at a glance. ``str()`` on a dataclass produces a long, class-name-
leaking form (``VibrationSettings(left_grip_strength=10, ...,
mode=<TriggerVibrationMode.NATIVE: 0>)``) that wraps across the modal and
is hostile to read.

:func:`format_field_value` returns a compact one-line human render
for the known restore-point payload dataclasses, and falls back to
``str(value)`` for scalars.

The helper is used in BOTH surfaces — :mod:`zd_app.services.restore_point_service`
(preview path + result-outcome path) — so the string the user sees in the
CONFIRM modal matches the string the user sees on the result page for the
same field-value pair.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence

from zd_app.i18n import t
from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ControllerButtonTarget,
    LightingMode,
    LightingSettings,
    LightingZone,
    MotionSettings,
    MotionMappingMode,
    MotionMappingTarget,
    POLLING_RATE_HZ,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    VibrationSettings,
    TriggerVibrationMode,
)


_LINEAR_3POINT_CURVE = (
    SensitivityAnchor(0, 0),
    SensitivityAnchor(50, 50),
    SensitivityAnchor(100, 100),
)

_ENUM_LABEL_KEYS: dict[Enum, str] = {
    TriggerMode.LONG: "restore_field.trigger_mode.long",
    TriggerMode.SHORT: "restore_field.trigger_mode.short",
    TriggerVibrationMode.NATIVE: "restore_field.vibration_mode.native",
    TriggerVibrationMode.STEREO_RESONANCE: "restore_field.vibration_mode.stereo_resonance",
    TriggerVibrationMode.TRIGGER_VIBRATION: "restore_field.vibration_mode.trigger_vibration",
    MotionMappingTarget.DISABLED: "controller.motion.target.disabled",
    MotionMappingTarget.LEFT_JOYSTICK: "controller.motion.target.left",
    MotionMappingTarget.RIGHT_JOYSTICK: "controller.motion.target.right",
    MotionMappingMode.INSTANT: "controller.motion.mode.instant",
    MotionMappingMode.CONTINUOUS: "controller.motion.mode.continuous",
    LightingMode.OFF: "restore_field.lighting_mode.off",
    LightingMode.ALWAYS_ON: "restore_field.lighting_mode.always_on",
    LightingMode.BREATH: "restore_field.lighting_mode.breath",
    LightingMode.FADE: "restore_field.lighting_mode.fade",
    LightingMode.FLOW: "restore_field.lighting_mode.flow",
    LightingZone.HOME: "restore_field.lighting_zone.home",
    LightingZone.LEFT_LIGHT: "restore_field.lighting_zone.left",
    LightingZone.RIGHT_LIGHT: "restore_field.lighting_zone.right",
}


def format_field_value(name: str, value: Any) -> str:
    """Render ``value`` as a compact one-line string for restore-diff display.

    Dispatches on ``value``'s concrete type for the known controller-payload
    dataclasses (:class:`VibrationSettings`, :class:`StickDeadzones`,
    :class:`TriggerSettings`, :class:`AxisInversion`, :class:`ButtonMapping`,
    :class:`BackPaddleBinding`, :class:`LightingSettings`,
    :class:`MotionSettings`, :class:`RgbColor`) plus the
    ``tuple[SensitivityAnchor, ...]`` shape used by sensitivity curves.
    Falls back to ``str(value)`` for scalars. Enums are rendered via a
    friendly label or at minimum their member name, never ``Class.MEMBER``.

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
    if isinstance(value, Enum):
        return _format_enum(value)
    return str(value)


def _format_vibration(value: VibrationSettings) -> str:
    return (
        f"{t('restore_field.vibration.grips')} "
        f"{value.left_grip_strength}/{value.right_grip_strength}; "
        f"{t('restore_field.vibration.triggers')} "
        f"{value.left_trigger_motor_strength}/{value.right_trigger_motor_strength}; "
        f"{_format_enum(value.mode)}"
    )


def _format_deadzones(value: StickDeadzones) -> str:
    return (
        f"{t('restore_field.deadzones.left')} "
        f"{value.left_center}/{value.left_outer}; "
        f"{t('restore_field.deadzones.right')} "
        f"{value.right_center}/{value.right_outer} "
        f"({t('restore_field.deadzones.center_outer')})"
    )


def _format_axis_inversion(value: AxisInversion) -> str:
    if value.x_inverted and value.y_inverted:
        return t("restore_field.axis.xy_inverted")
    if value.x_inverted:
        return t("restore_field.axis.x_inverted")
    if value.y_inverted:
        return t("restore_field.axis.y_inverted")
    return t("restore_field.axis.not_inverted")


def _format_trigger(value: TriggerSettings) -> str:
    return f"{value.range_min}-{value.range_max}%, {_format_enum(value.mode)}"


def _format_sensitivity_curve(value: Sequence[SensitivityAnchor]) -> str:
    if tuple(value) == _LINEAR_3POINT_CURVE:
        return t("restore_field.curve.linear")
    points = " ".join(f"{a.x}/{a.y}" for a in value)
    return f"{t('restore_field.curve.custom')} {points}"


def _format_button_mapping(value: ButtonMapping) -> str:
    if value.target_kind == 0x01 and value.target_low == 0x00:
        try:
            return f"→{_format_button_target(ControllerButtonTarget(value.target_value))}"
        except ValueError:
            return f"kind=0x01 val=0x{value.target_value:02x}"
    return (
        f"kind=0x{value.target_kind:02x} "
        f"low=0x{value.target_low:02x} "
        f"val=0x{value.target_value:02x}"
    )


def _format_back_paddle(value: BackPaddleBinding) -> str:
    if value.target is None:
        return t("controller.back_paddles.unbound")
    return f"→{_format_button_target(value.target)}"


def _format_lighting(value: LightingSettings) -> str:
    state = (
        t("restore_field.lighting.on")
        if value.light_on
        else t("restore_field.lighting.off")
    )
    return (
        f"{state}, {_format_enum(value.mode)}, "
        f"{t('restore_field.lighting.brightness')} {value.brightness_byte}, "
        f"{_format_rgb(value.color)}"
    )


def _format_motion(value: MotionSettings) -> str:
    return (
        f"{_format_enum(value.target)}, {_format_enum(value.mode)}, "
        f"{t('restore_field.motion.trigger_key')} 0x{value.trigger_key:02x}, "
        f"{t('restore_field.motion.sensitivity')} {value.sensitivity}"
    )


def _format_rgb(value: RgbColor) -> str:
    return f"RGB({value.r},{value.g},{value.b})"


def _format_button_target(value: ControllerButtonTarget) -> str:
    key = f"controller.back_paddles.target.{value.name}"
    rendered = t(key)
    return value.name if rendered == f"[{key}]" else rendered


def _format_enum(value: Enum) -> str:
    if isinstance(value, PollingRate):
        hz = POLLING_RATE_HZ.get(value)
        if hz is not None:
            return f"{hz} Hz"
    key = _ENUM_LABEL_KEYS.get(value)
    if key is None:
        return value.name
    rendered = t(key)
    return value.name if rendered == f"[{key}]" else rendered


__all__ = ["format_field_value"]
