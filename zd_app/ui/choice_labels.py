"""Localized display labels for Controller combo choices.

The Controller settings and protocol layers use the canonical English values in
this module's maps.  Dear PyGui receives the locale-specific display labels
only; converting a user selection back to canonical text happens at the UI
boundary before a service sees it.
"""

from __future__ import annotations

from zd_app.i18n import t


# Dict insertion order is the canonical combo order.  Keep the English values
# here as stable protocol/state discriminators; only their display strings are
# localized through the i18n keys.
CHOICE_LABEL_KEYS: dict[str, dict[str, str]] = {
    "vibration_mode": {
        "Native Trigger Vibration": "controller.choice.vibration_mode.native_trigger_vibration",
        "Stereo Resonance": "controller.choice.vibration_mode.stereo_resonance",
        "Trigger Vibration": "controller.choice.vibration_mode.trigger_vibration",
    },
    "trigger_mode": {
        "Short": "controller.choice.trigger_mode.short",
        "Long": "controller.choice.trigger_mode.long",
    },
    "lighting_zone": {
        "Home": "controller.choice.lighting_zone.home",
        "Left": "controller.choice.lighting_zone.left",
        "Right": "controller.choice.lighting_zone.right",
    },
    "lighting_mode": {
        "Off": "controller.choice.lighting_mode.off",
        "Always On": "controller.choice.lighting_mode.always_on",
        "Breath": "controller.choice.lighting_mode.breath",
        "Fade": "controller.choice.lighting_mode.fade",
        "Flow": "controller.choice.lighting_mode.flow",
    },
}


def display_items(domain: str) -> list[str]:
    """Return locale-specific display labels in canonical combo order."""

    return [to_display(domain, canonical) for canonical in CHOICE_LABEL_KEYS[domain]]


def to_display(domain: str, canonical: str) -> str:
    """Return a localized display label, preserving unknown values verbatim."""

    key = CHOICE_LABEL_KEYS[domain].get(canonical)
    return t(key) if key is not None else canonical


def to_canonical(domain: str, display: str) -> str:
    """Return a current-locale display label's canonical value.

    The reverse map is intentionally built at call time: a user can switch
    locale while the application is open, and no stale display labels should be
    accepted as canonical values.  Unknown strings pass through unchanged for
    forward compatibility and so English stays a byte-exact no-op.
    """

    display_to_canonical = {
        to_display(domain, canonical): canonical
        for canonical in CHOICE_LABEL_KEYS[domain]
    }
    return display_to_canonical.get(display, display)
