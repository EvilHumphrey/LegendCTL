"""Security containment for the hidden-controller transport.

The transient ``VID_20BC&PID_5080`` endpoint is not currently bound to the
public controller instance selected by the UI.  Until an authorizer-owned
handle/session design can prove that association, production hidden writes
stay unavailable.  This is deliberately not configurable by environment,
settings, or UI state.

The normal public ``VID_413D&PID_2104`` MI_02 settings transport is outside
this policy.
"""

from __future__ import annotations


HIDDEN_WRITE_DISABLED_MESSAGE = (
    "Hidden controller writes are disabled until the app can bind the "
    "transient endpoint to the selected physical controller. No packet was sent."
)


def hidden_writes_available() -> bool:
    """Return the fixed production policy for hidden-controller writes."""

    return False


__all__ = ["HIDDEN_WRITE_DISABLED_MESSAGE", "hidden_writes_available"]
