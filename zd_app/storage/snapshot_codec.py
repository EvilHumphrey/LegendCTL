"""ControllerSnapshot JSON codec for wrapper profile storage."""

from __future__ import annotations

from typing import Any

from zd_app.services.settings_service import (
    AxisInversion,
    BackPaddleBinding,
    ButtonMapping,
    ButtonSlot,
    ControllerSnapshot,
    ControllerButtonTarget,
    LightingMode,
    LightingSettings,
    LightingZone,
    MacroSlot,
    MotionMappingMode,
    MotionMappingTarget,
    MotionSettings,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    SensitivityAnchorTuple,
    SensitivityAnchorTuple8,
    StickDeadzones,
    SUPPORTED_CONTROLLER_BUTTON_TARGETS,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)

# Decode-time allowlist of button-remap target bytes. A valid mapping is the
# capture-confirmed ``01 00 TT`` shape where ``TT`` is a supported
# ``ControllerButtonTarget`` value — exactly what ``build_button_binding_payload``
# enforces at apply. Validating against this at decode means a tampered/foreign
# mapping is rejected at import preview, not silently at apply.
_SUPPORTED_BUTTON_TARGET_VALUES: frozenset[int] = frozenset(
    target.value for target in SUPPORTED_CONTROLLER_BUTTON_TARGETS
)


def snapshot_to_dict(snapshot: ControllerSnapshot) -> dict[str, Any]:
    return {
        "polling_rate": _enum_value(snapshot.polling_rate),
        "vibration": _vibration_to_dict(snapshot.vibration),
        "deadzones": _deadzones_to_dict(snapshot.deadzones),
        "axis_inversion_left": _axis_inversion_to_dict(snapshot.axis_inversion_left),
        "axis_inversion_right": _axis_inversion_to_dict(snapshot.axis_inversion_right),
        "sensitivity_left": _sensitivity_to_dict(snapshot.sensitivity_left),
        "sensitivity_right": _sensitivity_to_dict(snapshot.sensitivity_right),
        "sensitivity_left_8point": _sensitivity_8point_to_dict(
            snapshot.sensitivity_left_8point
        ),
        "sensitivity_right_8point": _sensitivity_8point_to_dict(
            snapshot.sensitivity_right_8point
        ),
        "trigger_left": _trigger_to_dict(snapshot.trigger_left),
        "trigger_right": _trigger_to_dict(snapshot.trigger_right),
        "button_bindings": {
            str(slot.value): _button_mapping_to_dict(mapping)
            for slot, mapping in (snapshot.button_bindings or {}).items()
        },
        "lighting_zones": {
            str(zone.value): _lighting_to_dict(settings)
            for zone, settings in (snapshot.lighting_zones or {}).items()
        },
        "motion_settings": _motion_to_dict(snapshot.motion_settings),
        "back_paddle_bindings": _back_paddle_bindings_to_dict(
            snapshot.back_paddle_bindings
        ),
        "step_size": snapshot.step_size,
    }


def snapshot_from_dict(payload: dict[str, Any]) -> ControllerSnapshot:
    # Deserialization boundary for untrusted profile JSON. Out-of-range values
    # raise ValueError in the per-field helpers below; structural type confusion
    # (wrong JSON shapes, a non-bool flag tripping AxisInversion.__post_init__)
    # surfaces as TypeError/AttributeError, and a missing required sub-key or
    # unknown enum name (e.g. MacroSlot["NOPE"]) surfaces as KeyError — all
    # normalized to ValueError so the storage layer (WrapperProfileStore.load /
    # list_profiles, which catch ValueError) rejects the profile instead of
    # crashing the caller.
    try:
        return _build_snapshot(payload)
    except KeyError as exc:
        # str(KeyError) is just the repr of the key; spell out what it means
        # so the rejection message names the offending key.
        raise ValueError(
            f"Invalid wrapper-profile snapshot: missing or unknown key {exc}"
        ) from exc
    except (TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid wrapper-profile snapshot: {exc}") from exc


def _build_snapshot(payload: dict[str, Any]) -> ControllerSnapshot:
    # SECURITY BOUNDARY — deny-by-default allowlist. This reads ONLY the known
    # snapshot keys below (each via an explicit ``payload.get`` / ``payload[...]``
    # and a validating helper); every other key in a hostile or hand-edited
    # profile is silently dropped because nothing here looks at it. This is what
    # makes Safe Import safe: an automation/foreign key can never round-trip into
    # a saved profile or the apply write-list. NEVER refactor this to splat the
    # payload (``ControllerSnapshot(**payload)`` / ``payload | defaults``) or to
    # iterate arbitrary keys — that would turn deny-by-default into
    # allow-by-default and breach the boundary. New fields get an explicit line.
    if not isinstance(payload, dict):
        raise ValueError(
            f"snapshot must be a JSON object, got {type(payload).__name__}"
        )
    return ControllerSnapshot(
        polling_rate=_enum_from(PollingRate, payload.get("polling_rate")),
        vibration=_vibration_from_dict(payload.get("vibration")),
        deadzones=_deadzones_from_dict(payload.get("deadzones")),
        axis_inversion_left=_axis_inversion_from_dict(payload.get("axis_inversion_left")),
        axis_inversion_right=_axis_inversion_from_dict(payload.get("axis_inversion_right")),
        sensitivity_left=_sensitivity_from_dict(payload.get("sensitivity_left")),
        sensitivity_right=_sensitivity_from_dict(payload.get("sensitivity_right")),
        sensitivity_left_8point=_sensitivity_8point_from_dict(
            payload.get("sensitivity_left_8point")
        ),
        sensitivity_right_8point=_sensitivity_8point_from_dict(
            payload.get("sensitivity_right_8point")
        ),
        trigger_left=_trigger_from_dict(payload.get("trigger_left")),
        trigger_right=_trigger_from_dict(payload.get("trigger_right")),
        button_bindings={
            ButtonSlot(_int_key(slot_key)): _button_mapping_from_dict(mapping_dict)
            for slot_key, mapping_dict in (payload.get("button_bindings") or {}).items()
        },
        lighting_zones={
            LightingZone(_int_key(zone_key)): _lighting_from_dict(settings_dict)
            for zone_key, settings_dict in (payload.get("lighting_zones") or {}).items()
        },
        motion_settings=_motion_from_dict(payload.get("motion_settings")),
        back_paddle_bindings=_back_paddle_bindings_from_dict(
            payload.get("back_paddle_bindings")
        ),
        step_size=_step_size_from(payload.get("step_size")),
    )


def _enum_value(value: Any) -> Any:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def _enum_from(enum_cls, raw: Any):
    if raw is None:
        return None
    # Reject bool before constructing the enum: ``True``/``False`` are ints in
    # Python (``True == 1``), so ``PollingRate(True)`` would silently coerce to a
    # valid member (HZ_250) instead of rejecting a garbled import. Bounded but
    # wrong — better to fail at the decode boundary than load a coerced value.
    # Matches the bool-first guard in ``_int_key`` / ``_percent`` / ``_byte`` /
    # ``_step_size_from``; one guard covers all six enum fields routed here.
    if isinstance(raw, bool):
        raise ValueError(f"{enum_cls.__name__} must not be a bool, got {raw!r}")
    return enum_cls(raw)


def _int_key(raw: Any) -> int:
    # Reject bool before the int check: ``True``/``False`` are ints in Python
    # (``True == 1``), so a ``{true: {...}}`` collection key would silently fold
    # onto slot 1. Matches the bool-first guard in ``_percent`` / ``_byte``.
    if isinstance(raw, bool):
        raise ValueError(f"collection key must not be a bool, got {raw!r}")
    if isinstance(raw, int):
        return raw
    value = int(raw)
    # Reject a non-canonical collection key ("05", " 5") that int() would fold
    # onto another key, silently dropping a member last-wins (e.g. "5" and "05"
    # both normalize to 5). Only a canonical decimal string round-trips, so an
    # ambiguous hand-edited / hostile profile is rejected via the codec's
    # existing ValueError path instead of loading lossily.
    if str(value) != raw:
        raise ValueError(
            f"non-canonical collection key {raw!r} (normalizes to {value})"
        )
    return value


def _percent(value: Any, name: str) -> int:
    """Reject an imported value that is not an int in [0, 100].

    Mirrors ``settings_service._validate_percent_int`` but raises ``ValueError``
    (not ``TypeError``) so storage call sites that catch ``ValueError`` reject
    the profile gracefully.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int in [0, 100], got {value!r}")
    if value < 0 or value > 100:
        raise ValueError(f"{name} must be in [0, 100], got {value!r}")
    return value


def _byte(value: Any, name: str) -> int:
    """Reject an imported value that is not an int in [0, 255].

    Mirrors ``settings_service._validate_byte_int`` but raises ``ValueError``.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int in [0, 255], got {value!r}")
    if value < 0 or value > 255:
        raise ValueError(f"{name} must be in [0, 255], got {value!r}")
    return value


def _step_size_from(raw: Any) -> int | None:
    """Deserialize the optional joystick step-size byte (1-255; None passes through)."""

    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"step_size must be an int in [1, 255], got {raw!r}")
    if raw < 1 or raw > 255:
        raise ValueError(f"step_size must be in [1, 255], got {raw!r}")
    return raw


def _vibration_to_dict(settings: VibrationSettings | None) -> dict[str, Any] | None:
    if settings is None:
        return None
    return {
        "left_grip_strength": settings.left_grip_strength,
        "right_grip_strength": settings.right_grip_strength,
        "left_trigger_motor_strength": settings.left_trigger_motor_strength,
        "right_trigger_motor_strength": settings.right_trigger_motor_strength,
        "mode": _enum_value(settings.mode),
    }


def _vibration_from_dict(payload: dict[str, Any] | None) -> VibrationSettings | None:
    if payload is None:
        return None
    return VibrationSettings(
        left_grip_strength=_percent(payload["left_grip_strength"], "vibration.left_grip_strength"),
        right_grip_strength=_percent(payload["right_grip_strength"], "vibration.right_grip_strength"),
        left_trigger_motor_strength=_percent(
            payload["left_trigger_motor_strength"], "vibration.left_trigger_motor_strength"
        ),
        right_trigger_motor_strength=_percent(
            payload["right_trigger_motor_strength"], "vibration.right_trigger_motor_strength"
        ),
        mode=_enum_from(TriggerVibrationMode, payload["mode"]),
    )


def _motion_to_dict(settings: MotionSettings | None) -> dict[str, Any] | None:
    if settings is None:
        return None
    return {
        "target": _enum_value(settings.target),
        "trigger_key": settings.trigger_key,
        "mode": _enum_value(settings.mode),
        "sensitivity": settings.sensitivity,
    }


def _motion_from_dict(payload: dict[str, Any] | None) -> MotionSettings | None:
    if payload is None:
        return None
    return MotionSettings(
        target=_enum_from(MotionMappingTarget, payload["target"]),
        trigger_key=_byte(payload["trigger_key"], "motion.trigger_key"),
        mode=_enum_from(MotionMappingMode, payload["mode"]),
        sensitivity=_byte(payload["sensitivity"], "motion.sensitivity"),
    )


def _deadzones_to_dict(deadzones: StickDeadzones | None) -> dict[str, Any] | None:
    if deadzones is None:
        return None
    return {
        "left_center": deadzones.left_center,
        "right_center": deadzones.right_center,
        "left_outer": deadzones.left_outer,
        "right_outer": deadzones.right_outer,
    }


def _deadzones_from_dict(payload: dict[str, Any] | None) -> StickDeadzones | None:
    if payload is None:
        return None
    return StickDeadzones(
        left_center=_percent(payload["left_center"], "deadzones.left_center"),
        right_center=_percent(payload["right_center"], "deadzones.right_center"),
        left_outer=_percent(payload["left_outer"], "deadzones.left_outer"),
        right_outer=_percent(payload["right_outer"], "deadzones.right_outer"),
    )


def _axis_inversion_to_dict(inversion: AxisInversion | None) -> dict[str, Any] | None:
    if inversion is None:
        return None
    return {
        "x_inverted": inversion.x_inverted,
        "y_inverted": inversion.y_inverted,
    }


def _axis_inversion_from_dict(payload: dict[str, Any] | None) -> AxisInversion | None:
    if payload is None:
        return None
    return AxisInversion(
        x_inverted=payload["x_inverted"],
        y_inverted=payload["y_inverted"],
    )


def _sensitivity_to_dict(anchors: SensitivityAnchorTuple | None) -> list[dict[str, Any]] | None:
    if anchors is None:
        return None
    return [{"x": anchor.x, "y": anchor.y} for anchor in anchors]


def _sensitivity_from_dict(payload: list[dict[str, Any]] | None) -> SensitivityAnchorTuple | None:
    if payload is None:
        return None
    return tuple(
        SensitivityAnchor(
            x=_percent(anchor["x"], "sensitivity.x"),
            y=_percent(anchor["y"], "sensitivity.y"),
        )
        for anchor in payload
    )  # type: ignore[return-value]


def _sensitivity_8point_to_dict(
    anchors: SensitivityAnchorTuple8 | None,
) -> list[dict[str, Any]] | None:
    """Serialize the 1.2.9 / fw-1.24 8-point curve (HID cat 0x86).

    Same wire shape as :func:`_sensitivity_to_dict` — an ordered list of
    ``{"x", "y"}`` anchors — kept as a separate helper so the field stays
    typed against :data:`SensitivityAnchorTuple8`. ``None`` (device isn't
    8-point-capable, or a pre-1.2.9 profile) passes straight through.
    """

    if anchors is None:
        return None
    return [{"x": anchor.x, "y": anchor.y} for anchor in anchors]


def _sensitivity_8point_from_dict(
    payload: list[dict[str, Any]] | None,
) -> SensitivityAnchorTuple8 | None:
    """Deserialize the optional 8-point curve.

    Mirrors :func:`_sensitivity_from_dict`: each anchor's x/y is range-checked
    as a percent here, while curve-shape semantics (exactly 8 anchors,
    non-decreasing) are enforced later by the apply path's
    ``_validate_sensitivity_anchors_8point`` — matching how the 3-point codec
    defers to its own validator. ``None`` (key absent in a pre-1.2.9 profile)
    passes straight through, which is what makes old snapshots load
    backward-compatibly with ``sensitivity_*_8point = None``.
    """

    if payload is None:
        return None
    return tuple(
        SensitivityAnchor(
            x=_percent(anchor["x"], "sensitivity_8point.x"),
            y=_percent(anchor["y"], "sensitivity_8point.y"),
        )
        for anchor in payload
    )  # type: ignore[return-value]


def _trigger_to_dict(settings: TriggerSettings | None) -> dict[str, Any] | None:
    if settings is None:
        return None
    return {
        "range_min": settings.range_min,
        "range_max": settings.range_max,
        "mode": _enum_value(settings.mode),
    }


def _trigger_from_dict(payload: dict[str, Any] | None) -> TriggerSettings | None:
    if payload is None:
        return None
    range_min = _percent(payload["range_min"], "trigger.range_min")
    range_max = _percent(payload["range_max"], "trigger.range_max")
    if range_min > range_max:
        raise ValueError(
            f"trigger range_min ({range_min}) must be <= range_max ({range_max})"
        )
    return TriggerSettings(
        range_min=range_min,
        range_max=range_max,
        mode=_enum_from(TriggerMode, payload["mode"]),
    )


def _button_mapping_to_dict(mapping: ButtonMapping) -> dict[str, Any]:
    return {
        "target_kind": mapping.target_kind,
        "target_low": mapping.target_low,
        "target_value": mapping.target_value,
    }


def _button_mapping_from_dict(payload: dict[str, Any]) -> ButtonMapping:
    target_kind = _byte(payload["target_kind"], "button.target_kind")
    target_low = _byte(payload["target_low"], "button.target_low")
    target_value = _byte(payload["target_value"], "button.target_value")
    # Decode-boundary validation, mirroring the back-paddle target check below
    # and the apply-time guard in ``build_button_binding_payload``: only the
    # capture-confirmed ``01 00 TT`` controller-button shape is supported, so a
    # mapping that is in-byte-range but not a real remap fails here (import
    # preview ok=False) instead of being silently dropped at apply.
    if target_kind != 0x01 or target_low != 0x00:
        raise ValueError(
            f"unsupported button mapping {target_kind:#04x} {target_low:#04x} "
            f"{target_value:#04x}: only 01 00 TT controller-button remaps are supported"
        )
    if target_value not in _SUPPORTED_BUTTON_TARGET_VALUES:
        raise ValueError(
            f"unsupported button mapping target value {target_value:#04x}"
        )
    return ButtonMapping(
        target_kind=target_kind,
        target_low=target_low,
        target_value=target_value,
    )


def _back_paddle_bindings_to_dict(
    bindings: dict[MacroSlot, BackPaddleBinding] | None,
) -> dict[str, Any]:
    if not bindings:
        return {}
    return {
        slot.name: {"target": binding.target.name if binding.target is not None else None}
        for slot, binding in bindings.items()
    }


def _back_paddle_bindings_from_dict(
    payload: dict[str, Any] | None,
) -> dict[MacroSlot, BackPaddleBinding]:
    if not payload:
        return {}
    bindings: dict[MacroSlot, BackPaddleBinding] = {}
    for slot_name, binding_payload in payload.items():
        slot = MacroSlot[slot_name]
        # Each binding must be a mapping carrying an explicit ``target`` key.
        # The old ``binding_payload.get("target") if binding_payload else None``
        # treated any falsy payload (``false`` / ``""`` / ``[]`` / ``{}``) as a
        # silent unbind, and the truthiness test on the target value coerced
        # ``0`` / ``""`` to unbound while a truthy-invalid name raised — an
        # inconsistent boundary. Require the shape and use ``is None`` so only an
        # explicit JSON ``null`` unbinds; a known name binds; anything else is
        # rejected.
        if not isinstance(binding_payload, dict) or "target" not in binding_payload:
            raise ValueError(
                f"back-paddle binding {slot_name!r} must be a mapping with a "
                f"'target' key, got {binding_payload!r}"
            )
        target_name = binding_payload["target"]
        if target_name is None:
            target = None
        elif not isinstance(target_name, str):
            raise ValueError(
                f"back-paddle binding {slot_name!r} target must be a string name "
                f"or null, got {target_name!r}"
            )
        else:
            target = ControllerButtonTarget[target_name]
        bindings[slot] = BackPaddleBinding(target=target)
    return bindings


def _lighting_to_dict(settings: LightingSettings) -> dict[str, Any]:
    return {
        "light_on": settings.light_on,
        "mode": _enum_value(settings.mode),
        "brightness_byte": settings.brightness_byte,
        "color": _rgb_to_dict(settings.color),
    }


def _lighting_from_dict(payload: dict[str, Any]) -> LightingSettings:
    # ``LightingSettings`` has no __post_init__, so (unlike AxisInversion) it
    # won't reject a non-bool flag on its own — type-check ``light_on`` here so a
    # truthy int / string can't slip past the decode boundary.
    light_on = payload["light_on"]
    if not isinstance(light_on, bool):
        raise ValueError(f"lighting.light_on must be a bool, got {light_on!r}")
    return LightingSettings(
        light_on=light_on,
        mode=_enum_from(LightingMode, payload["mode"]),
        brightness_byte=_byte(payload["brightness_byte"], "lighting.brightness_byte"),
        color=_rgb_from_dict(payload["color"]),
    )


def _rgb_to_dict(color: RgbColor) -> dict[str, Any]:
    return {
        "r": color.r,
        "g": color.g,
        "b": color.b,
    }


def _rgb_from_dict(payload: dict[str, Any]) -> RgbColor:
    return RgbColor(
        r=_byte(payload["r"], "rgb.r"),
        g=_byte(payload["g"], "rgb.g"),
        b=_byte(payload["b"], "rgb.b"),
    )
