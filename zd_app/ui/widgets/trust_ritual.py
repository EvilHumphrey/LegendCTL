"""Trust ritual card shown on first_readable_connect.

A non-modal floating overlay rendered at the top of the main window when
the controller is first successfully read this session. Communicates the
wrapper's trust posture inline — "local only", "config only", "verified
writes" — so the user gets a brief premium confirmation moment rather
than a silent state change.

Gating: rendered once per (``product_string``, app session). The per-session
set lives on :class:`AppShell` so it is shared with the
``first_readable_connect`` restore-point capture and survives screen
rebuilds. Reconnecting the same controller inside one session does NOT
re-render.

Fade-out: :func:`tick_trust_ritual_card` runs each frame from
:meth:`AppShell._tick` and dismisses the card after
:data:`TRUST_RITUAL_AUTOFADE_S` seconds of visibility. The Dismiss
button dismisses it immediately.

Design constraints:

- Premium feel, not gamer-RGB. Plain text, no flashy animations.
- Non-blocking. ``modal=False`` so the rest of the UI stays interactive.
- Session-scoped. Reset only on app restart.
- No anti-cheat compatibility language on the card itself — the card is
  a ritual moment, not full disclosure. Full anti-cheat language lives
  elsewhere in the app's trust copy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Set

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.storage.restore_point_models import DeviceIdentity


logger = logging.getLogger(__name__)


TRUST_RITUAL_WINDOW_TAG = "trust_ritual_card"
TRUST_RITUAL_TITLE_TAG = "trust_ritual_title_text"
TRUST_RITUAL_CONNECTION_TAG = "trust_ritual_connection_text"
TRUST_RITUAL_LOCAL_ONLY_TAG = "trust_ritual_local_only_text"
TRUST_RITUAL_CONFIG_ONLY_TAG = "trust_ritual_config_only_text"
TRUST_RITUAL_FIRMWARE_SLOT_TAG = "trust_ritual_firmware_slot_text"
TRUST_RITUAL_VERIFIED_WRITES_TAG = "trust_ritual_verified_writes_text"
TRUST_RITUAL_DISMISS_TAG = "trust_ritual_dismiss_button"


TRUST_RITUAL_AUTOFADE_S = 8.0
"""Card auto-dismisses after this many seconds of visibility."""


@dataclass
class TrustRitualState:
    """Per-session state for the trust ritual card.

    ``shown_at`` is the ``time.monotonic()`` moment the card became
    visible (``None`` when the card is not currently shown). The fade
    timer compares against this.

    ``rendered_keys`` is the set of ``product_string`` identity keys we
    have already rendered the card for this session. Mirrors
    :attr:`AppShell._first_connect_captured` semantics so a reconnect of
    the same controller inside one session does not re-trigger.

    ``pending_render`` holds an identity whose render was deferred
    because DPG wasn't ready when :func:`show_trust_ritual_card` fired.
    :func:`drain_pending_trust_ritual` consumes it once DPG is up.
    """

    shown_at: Optional[float] = None
    rendered_keys: Set[str] = field(default_factory=set)
    pending_render: Optional[DeviceIdentity] = None


def _ensure_state(shell) -> TrustRitualState:
    """Return ``shell._trust_ritual_state``, creating it on first call."""

    state = getattr(shell, "_trust_ritual_state", None)
    if state is None:
        state = TrustRitualState()
        shell._trust_ritual_state = state
    return state


def _transport_label(transport: str) -> str:
    """Map a ``device_service.state.connection_mode`` value to user copy.

    Reuses the ``status.transport.*`` keys the top status bar already
    uses (see ``_top_connection_mode_label`` in app_shell) so the wording
    on the card matches what the user sees in the header.
    """

    normalized = (transport or "").strip().lower()
    key_by_mode = {
        "usb": "status.transport.usb",
        "bt": "status.transport.bt",
        "bluetooth": "status.transport.bt",
        "xinput": "status.transport.xinput",
    }
    key = key_by_mode.get(normalized, "status.transport.unknown")
    return t(key)


def _active_slot_for_card(shell) -> Optional[int]:
    """Return the verified active onboard slot (1-4), or ``None``.

    Mirrors the "Not verified" gate from ``_localized_active_config_label``
    in app_shell: when the device-service summary says active_profile is
    ``"unknown"`` we treat the slot as not readable and omit the line
    rather than render "Config 0" / "Config 1" based on a stale default.
    """

    state = shell.device_service.state
    summary_sources = getattr(state, "summary_sources", {}) or {}
    if summary_sources.get("active_profile") == "unknown":
        return None
    slot = getattr(state, "active_onboard_profile", None)
    if not isinstance(slot, int) or slot < 1:
        return None
    return slot


def _build_trust_ritual_card(shell, identity: DeviceIdentity) -> None:
    """Render (or re-render) the trust ritual card into the main viewport.

    Idempotent: if a previous card window with the same tag exists, it is
    deleted before the new one is created so a stale instance from a
    prior render cycle never lingers.

    Caller must check :attr:`AppShell._dpg_context_ready` — this helper
    skips the dpg calls silently when no context is active so it remains
    safe to invoke from tests that do not stand up DPG.
    """

    if not getattr(shell, "_dpg_context_ready", False):
        return

    if dpg.does_item_exist(TRUST_RITUAL_WINDOW_TAG):
        dpg.delete_item(TRUST_RITUAL_WINDOW_TAG)

    transport_raw = getattr(shell.device_service.state, "connection_mode", "Unknown")
    transport_label = _transport_label(transport_raw)
    firmware = identity.firmware_version
    slot = _active_slot_for_card(shell)
    colors = getattr(shell, "COLORS", {})
    accent = colors.get("accent")
    muted = colors.get("muted")

    with dpg.window(
        tag=TRUST_RITUAL_WINDOW_TAG,
        label=t("trust_ritual.title"),
        modal=False,
        no_resize=True,
        no_collapse=True,
        no_close=True,
        width=520,
        height=210,
        pos=(20, 56),
    ):
        title_kwargs = {"tag": TRUST_RITUAL_TITLE_TAG}
        if accent is not None:
            title_kwargs["color"] = accent
        dpg.add_text(t("trust_ritual.title"), **title_kwargs)
        dpg.add_spacer(height=4)
        dpg.add_text(
            t("trust_ritual.connection", transport=transport_label),
            tag=TRUST_RITUAL_CONNECTION_TAG,
            wrap=480,
        )
        dpg.add_text(
            t("trust_ritual.local_only"),
            tag=TRUST_RITUAL_LOCAL_ONLY_TAG,
            wrap=480,
        )
        dpg.add_text(
            t("trust_ritual.config_only"),
            tag=TRUST_RITUAL_CONFIG_ONLY_TAG,
            wrap=480,
        )
        if firmware and slot is not None:
            fw_kwargs = {"tag": TRUST_RITUAL_FIRMWARE_SLOT_TAG, "wrap": 480}
            if muted is not None:
                fw_kwargs["color"] = muted
            dpg.add_text(
                t("trust_ritual.firmware_slot", fw=firmware, slot=slot),
                **fw_kwargs,
            )
        dpg.add_text(
            t("trust_ritual.verified_writes"),
            tag=TRUST_RITUAL_VERIFIED_WRITES_TAG,
            wrap=480,
        )
        dpg.add_spacer(height=8)
        dpg.add_button(
            label=t("trust_ritual.dismiss"),
            tag=TRUST_RITUAL_DISMISS_TAG,
            width=120,
            callback=lambda *_args: dismiss_trust_ritual_card(shell),
        )


def show_trust_ritual_card(shell, identity: DeviceIdentity) -> bool:
    """Show the card for ``identity`` if it has not been shown this session.

    Returns True if a fresh render happened, False if either the
    per-session gate suppressed it OR the render was deferred because
    DPG isn't ready yet. In the deferred case the identity is stashed
    on ``state.pending_render`` so :func:`drain_pending_trust_ritual`
    can fire it once DPG is up — the per-session ``rendered_keys`` gate
    is NOT advanced until a real render actually happens, so a deferred
    call doesn't poison the gate.
    """

    state = _ensure_state(shell)
    key = identity.product_string or "__unknown__"
    if key in state.rendered_keys:
        return False
    if not getattr(shell, "_dpg_context_ready", False):
        # Defer rather than silently advancing the gate. The earliest
        # first_readable_connect can fire during ``AppShell.run()`` is
        # the post-DPG-init read, but defensive stashing covers any
        # future caller that hits this path before context is up.
        state.pending_render = identity
        return False
    _build_trust_ritual_card(shell, identity)
    state.shown_at = time.monotonic()
    state.rendered_keys.add(key)
    if state.pending_render is not None and (
        state.pending_render.product_string == identity.product_string
    ):
        state.pending_render = None
    return True


def drain_pending_trust_ritual(shell) -> bool:
    """Render a deferred trust ritual card once DPG is ready.

    Called by :meth:`AppShell.run` immediately after the DPG context is
    initialized so any ``show_trust_ritual_card`` call that arrived
    before context-ready (e.g. during the synchronous startup
    refresh_state path) gets its render. No-op when there is nothing
    pending or DPG still isn't ready.

    Returns True if a deferred render was drained, False otherwise.
    """

    state = getattr(shell, "_trust_ritual_state", None)
    if state is None or state.pending_render is None:
        return False
    if not getattr(shell, "_dpg_context_ready", False):
        return False
    identity = state.pending_render
    state.pending_render = None
    return show_trust_ritual_card(shell, identity)


def dismiss_trust_ritual_card(shell) -> None:
    """Hide the card immediately (button callback + fade-out path).

    Does NOT clear ``rendered_keys`` — once dismissed for a given
    identity, the card stays gone for the rest of the session.
    """

    state = _ensure_state(shell)
    state.shown_at = None
    if not getattr(shell, "_dpg_context_ready", False):
        return
    if dpg.does_item_exist(TRUST_RITUAL_WINDOW_TAG):
        dpg.delete_item(TRUST_RITUAL_WINDOW_TAG)


def tick_trust_ritual_card(shell, *, now: Optional[float] = None) -> None:
    """Auto-fade tick — called from :meth:`AppShell._tick` each frame.

    Dismisses the card once :data:`TRUST_RITUAL_AUTOFADE_S` seconds have
    elapsed since :func:`show_trust_ritual_card` set ``shown_at``.
    ``now`` defaults to ``time.monotonic()``; tests pass an explicit
    value to drive the fade deterministically without sleeping.
    """

    state = _ensure_state(shell)
    if state.shown_at is None:
        return
    if now is None:
        now = time.monotonic()
    if now - state.shown_at >= TRUST_RITUAL_AUTOFADE_S:
        dismiss_trust_ritual_card(shell)


def trust_ritual_is_visible(shell) -> bool:
    """Helper for tests: True when the card window currently exists."""

    if not getattr(shell, "_dpg_context_ready", False):
        return False
    return dpg.does_item_exist(TRUST_RITUAL_WINDOW_TAG)


__all__ = [
    "TRUST_RITUAL_AUTOFADE_S",
    "TRUST_RITUAL_WINDOW_TAG",
    "TRUST_RITUAL_DISMISS_TAG",
    "TRUST_RITUAL_TITLE_TAG",
    "TRUST_RITUAL_CONNECTION_TAG",
    "TRUST_RITUAL_LOCAL_ONLY_TAG",
    "TRUST_RITUAL_CONFIG_ONLY_TAG",
    "TRUST_RITUAL_FIRMWARE_SLOT_TAG",
    "TRUST_RITUAL_VERIFIED_WRITES_TAG",
    "TrustRitualState",
    "_build_trust_ritual_card",
    "show_trust_ritual_card",
    "drain_pending_trust_ritual",
    "dismiss_trust_ritual_card",
    "tick_trust_ritual_card",
    "trust_ritual_is_visible",
]
