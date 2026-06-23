"""Tests for the trust ritual card.

Feature: trust-ritual-at-connect (2026-05-26). The card surfaces a brief
"local only / config only / verified writes" panel the first time the
controller reads successfully each session. See
:mod:`zd_app.ui.widgets.trust_ritual` for the design.

Coverage:

- ``_build_trust_ritual_card`` renders all required lines (title,
  connection, local-only, config-only, verified-writes, dismiss button)
  with valid identity input.
- ``show_trust_ritual_card`` gating: 2nd call with the same
  ``product_string`` does not re-render.
- ``tick_trust_ritual_card`` fade-out: card is hidden once the autofade
  threshold elapses (driven by an explicit ``now=`` to avoid sleeping).
- Dismiss button callback hides the card immediately.
- ``AppShell._maybe_capture_first_readable_connect`` invokes
  ``show_trust_ritual_card`` AFTER ``first_readable_connect`` actually
  fires (RP capture succeeded) — gating with the RP session set.
- i18n parity: en + zh-CN both define the same ``trust_ritual.*`` keys.
- Forbidden-phrase extension: trust_ritual.* keys do not use any of the
  blocked "factory backup" vocabulary.

DPG-bearing tests stand up a real context inside ``setUp`` so the
widget's ``dpg.window`` + ``dpg.add_button`` calls execute against a
live tree. Pure logic tests (gating, parity, forbidden-phrase) skip the
context entirely.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.i18n import set_locale, t
from zd_app.storage.restore_point_models import (
    DeviceIdentity,
    IdentityConfidence,
    RestorePointTrigger,
)
from zd_app.ui.widgets import trust_ritual


_LOCALES_DIR = Path("zd_app/i18n/locales")
_TRUST_RITUAL_KEYS = (
    "trust_ritual.title",
    "trust_ritual.connection",
    "trust_ritual.local_only",
    "trust_ritual.config_only",
    "trust_ritual.firmware_slot",
    "trust_ritual.verified_writes",
    "trust_ritual.dismiss",
)


# ---------------------------------------------------------------------------
# Identity factories
# ---------------------------------------------------------------------------


def _identity(
    *,
    product: str | None = "ZD Ultimate Legend",
    firmware: str | None = "1.18",
    confidence: IdentityConfidence = IdentityConfidence.READABLE,
) -> DeviceIdentity:
    return DeviceIdentity(
        vid="413D",
        pid="2104",
        product_string=product,
        firmware_version=firmware,
        identity_confidence=confidence,
    )


class _RecordingService:
    """Minimal stand-in for ``RestorePointService`` that records captures.

    Returns a populated namespace for every ``capture(...)`` call so the
    shell's first_readable_connect handler treats the capture as having
    succeeded (and therefore proceeds to the trust ritual hook).
    """

    def __init__(self) -> None:
        self.captures: list[str] = []
        self._counter = 0
        self.list_with_skipped = MagicMock(return_value=([], []))

    def capture(
        self,
        trigger: RestorePointTrigger,
        *,
        title=None,
        device_identity=None,
        fresh_read_max_age_s: float = 30.0,
        cached_snapshot=None,
        cached_snapshot_ts=None,
    ):
        self._counter += 1
        self.captures.append(trigger.type)
        return SimpleNamespace(
            id=f"rp_trust_{self._counter}",
            title=title or f"{trigger.source_label} — recorded",
            trigger=trigger,
        )

    def restore(self, *args, **kwargs):
        raise AssertionError("restore() unexpected in trust ritual tests")


def _make_shell_for_card(*, locale: str = "en"):
    """Build a shell wired to render the trust card via DPG.

    Caller is responsible for an outer ``dpg.create_context()`` /
    ``dpg.destroy_context()``. The shell's ``_dpg_context_ready`` flag is
    flipped to True so the widget proceeds past its safety guard.
    """

    set_locale(locale)
    shell = make_shell(
        settings_service=MagicMock(),
        restore_point_service=_RecordingService(),
    )
    shell._dpg_context_ready = True
    # The shell mock leaves COLORS as a real dict via the AppShell
    # construction path; if not, fall back to a sentinel that supports
    # .get(...) so the widget's accent / muted lookups stay safe.
    if not hasattr(shell, "COLORS") or shell.COLORS is None:
        shell.COLORS = {}
    shell.device_service.state.connection_mode = "USB"
    shell.device_service.state.product_name = "ZD Ultimate Legend"
    shell.device_service.state.active_onboard_profile = 1
    shell.device_service.state.summary_sources = {"active_profile": "controller"}
    return shell


# ---------------------------------------------------------------------------
# DPG-bearing render tests
# ---------------------------------------------------------------------------


class BuildCardRendersAllLinesTests(unittest.TestCase):
    """_build_trust_ritual_card renders the six required lines + button."""

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_card_renders_all_lines_with_full_identity(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity()
        trust_ritual._build_trust_ritual_card(shell, identity)

        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_WINDOW_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_TITLE_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_CONNECTION_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_LOCAL_ONLY_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_CONFIG_ONLY_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_VERIFIED_WRITES_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_FIRMWARE_SLOT_TAG))
        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_DISMISS_TAG))

        self.assertEqual(
            dpg.get_value(trust_ritual.TRUST_RITUAL_TITLE_TAG),
            t("trust_ritual.title"),
        )
        # Transport label is interpolated; verify the USB substring lands
        # in the rendered connection line rather than the raw {transport}
        # placeholder.
        self.assertIn("USB", dpg.get_value(trust_ritual.TRUST_RITUAL_CONNECTION_TAG))
        self.assertNotIn(
            "{transport}",
            dpg.get_value(trust_ritual.TRUST_RITUAL_CONNECTION_TAG),
        )
        firmware_text = dpg.get_value(trust_ritual.TRUST_RITUAL_FIRMWARE_SLOT_TAG)
        self.assertIn("1.18", firmware_text)
        self.assertIn("1", firmware_text)
        self.assertNotIn("{fw}", firmware_text)
        self.assertNotIn("{slot}", firmware_text)

    def test_firmware_slot_line_skipped_when_either_unknown(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity(firmware=None)
        trust_ritual._build_trust_ritual_card(shell, identity)

        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_WINDOW_TAG))
        self.assertFalse(
            dpg.does_item_exist(trust_ritual.TRUST_RITUAL_FIRMWARE_SLOT_TAG)
        )

    def test_firmware_slot_line_skipped_when_active_profile_unverified(self) -> None:
        shell = _make_shell_for_card()
        shell.device_service.state.summary_sources = {"active_profile": "unknown"}
        identity = _identity()
        trust_ritual._build_trust_ritual_card(shell, identity)

        self.assertTrue(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_WINDOW_TAG))
        self.assertFalse(
            dpg.does_item_exist(trust_ritual.TRUST_RITUAL_FIRMWARE_SLOT_TAG)
        )

    def test_dismiss_button_hides_card_immediately(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity()
        trust_ritual.show_trust_ritual_card(shell, identity)
        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))

        callback = dpg.get_item_configuration(
            trust_ritual.TRUST_RITUAL_DISMISS_TAG
        )["callback"]
        callback()

        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))
        self.assertIsNone(shell._trust_ritual_state.shown_at)
        # Dismiss must NOT clear the rendered_keys gate — once shown for
        # an identity, the same product_string should not re-render this
        # session.
        self.assertIn(
            identity.product_string, shell._trust_ritual_state.rendered_keys
        )


class FadeOutTickTests(unittest.TestCase):
    """tick_trust_ritual_card dismisses the card once the autofade window elapses."""

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_tick_before_threshold_keeps_card_visible(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity()
        trust_ritual.show_trust_ritual_card(shell, identity)
        shown_at = shell._trust_ritual_state.shown_at
        self.assertIsNotNone(shown_at)

        trust_ritual.tick_trust_ritual_card(
            shell, now=shown_at + trust_ritual.TRUST_RITUAL_AUTOFADE_S - 0.5
        )

        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))
        self.assertEqual(shell._trust_ritual_state.shown_at, shown_at)

    def test_tick_at_threshold_hides_card(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity()
        trust_ritual.show_trust_ritual_card(shell, identity)
        shown_at = shell._trust_ritual_state.shown_at

        trust_ritual.tick_trust_ritual_card(
            shell, now=shown_at + trust_ritual.TRUST_RITUAL_AUTOFADE_S
        )

        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))
        self.assertIsNone(shell._trust_ritual_state.shown_at)

    def test_tick_when_not_shown_is_a_no_op(self) -> None:
        shell = _make_shell_for_card()
        # No show_trust_ritual_card call; shown_at remains None.
        trust_ritual.tick_trust_ritual_card(shell, now=1000.0)
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))


class OncePerSessionGateTests(unittest.TestCase):
    """show_trust_ritual_card suppresses subsequent calls for same identity."""

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_second_call_same_product_string_does_not_re_render(self) -> None:
        shell = _make_shell_for_card()
        identity = _identity()
        self.assertTrue(trust_ritual.show_trust_ritual_card(shell, identity))
        trust_ritual.dismiss_trust_ritual_card(shell)

        # Same product_string → second call must skip.
        self.assertFalse(trust_ritual.show_trust_ritual_card(shell, identity))
        # Card should remain hidden (dismissed and not re-shown).
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))

    def test_different_product_string_renders_again(self) -> None:
        shell = _make_shell_for_card()
        identity_a = _identity(product="ZD Ultimate Legend")
        identity_b = _identity(product="Other ZD Variant")
        self.assertTrue(trust_ritual.show_trust_ritual_card(shell, identity_a))
        trust_ritual.dismiss_trust_ritual_card(shell)
        self.assertTrue(trust_ritual.show_trust_ritual_card(shell, identity_b))


# ---------------------------------------------------------------------------
# AppShell integration: first_readable_connect → trust ritual
# ---------------------------------------------------------------------------


class FirstReadableConnectIntegrationTests(unittest.TestCase):
    """``_maybe_capture_first_readable_connect`` triggers the trust ritual.

    The shell's first_readable_connect helper is the only public entry
    point that should drive the trust ritual; verifying integration here
    means a future refactor that moves the hook elsewhere can't silently
    drop the card.
    """

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_first_connect_fires_trust_ritual_once(self) -> None:
        shell = _make_shell_for_card()
        # _current_device_identity reads from device_service.state; the
        # helper expects product_name + firmware_version to land in the
        # right places.
        shell.device_service.state.product_name = "ZD Ultimate Legend"
        shell.device_service.state.firmware_version = "1.18"

        shell._maybe_capture_first_readable_connect()
        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))

        # Second call for the same product_string: gate suppresses both
        # the RP capture and the trust ritual re-render.
        trust_ritual.dismiss_trust_ritual_card(shell)
        shell._maybe_capture_first_readable_connect()
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))

    def test_trust_ritual_not_shown_when_rp_capture_fails(self) -> None:
        # If the RP capture path can't even successfully snapshot the
        # device, the trust statement is premature — no card. Mimic that
        # by forcing ``capture`` to return None.
        shell = _make_shell_for_card()
        shell.device_service.state.product_name = "ZD Ultimate Legend"
        shell.device_service.state.firmware_version = "1.18"
        shell.restore_point_service.capture = MagicMock(return_value=None)

        shell._maybe_capture_first_readable_connect()
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))


# ---------------------------------------------------------------------------
# DPG-not-ready deferral + drain
# ---------------------------------------------------------------------------


class DpgNotReadyDeferralTests(unittest.TestCase):
    """show_trust_ritual_card defers when DPG context isn't ready yet.

    Regression for the deferred-render finding: the original
    implementation silently early-returned inside ``_build_trust_ritual_card``
    when ``_dpg_context_ready`` was False but unconditionally advanced
    ``rendered_keys``, so the once-per-session gate poisoned itself and
    the card never rendered on real launches. The fix stashes the
    identity on ``pending_render`` and waits for ``drain_pending_trust_ritual``.
    """

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_show_without_dpg_ready_stashes_pending_does_not_mark_rendered(
        self,
    ) -> None:
        shell = _make_shell_for_card()
        shell._dpg_context_ready = False
        identity = _identity()

        result = trust_ritual.show_trust_ritual_card(shell, identity)

        self.assertFalse(result)
        self.assertEqual(shell._trust_ritual_state.pending_render, identity)
        self.assertEqual(shell._trust_ritual_state.rendered_keys, set())
        self.assertIsNone(shell._trust_ritual_state.shown_at)
        self.assertFalse(dpg.does_item_exist(trust_ritual.TRUST_RITUAL_WINDOW_TAG))

    def test_drain_pending_after_dpg_ready_renders_and_marks(self) -> None:
        shell = _make_shell_for_card()
        shell._dpg_context_ready = False
        identity = _identity()
        trust_ritual.show_trust_ritual_card(shell, identity)
        # Sanity: stash worked, no render.
        self.assertEqual(shell._trust_ritual_state.pending_render, identity)
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))

        # Flip the flag and drain — simulates AppShell.run() after
        # dpg.create_context().
        shell._dpg_context_ready = True
        drained = trust_ritual.drain_pending_trust_ritual(shell)

        self.assertTrue(drained)
        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))
        self.assertIn(
            identity.product_string, shell._trust_ritual_state.rendered_keys
        )
        self.assertIsNone(shell._trust_ritual_state.pending_render)
        self.assertIsNotNone(shell._trust_ritual_state.shown_at)

    def test_drain_is_noop_when_nothing_pending(self) -> None:
        shell = _make_shell_for_card()
        # Fresh state; drain should report nothing happened.
        drained = trust_ritual.drain_pending_trust_ritual(shell)
        self.assertFalse(drained)
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))

    def test_drain_is_noop_when_dpg_still_not_ready(self) -> None:
        shell = _make_shell_for_card()
        shell._dpg_context_ready = False
        identity = _identity()
        trust_ritual.show_trust_ritual_card(shell, identity)
        # Don't flip the flag — drain must defer too.
        drained = trust_ritual.drain_pending_trust_ritual(shell)
        self.assertFalse(drained)
        self.assertEqual(shell._trust_ritual_state.pending_render, identity)
        self.assertEqual(shell._trust_ritual_state.rendered_keys, set())

    def test_per_session_gate_unchanged_when_dpg_ready_throughout(self) -> None:
        # Existing once-per-session-per-product gate still works in the
        # normal DPG-ready path — two consecutive shows render once.
        shell = _make_shell_for_card()
        identity = _identity()
        self.assertTrue(trust_ritual.show_trust_ritual_card(shell, identity))
        trust_ritual.dismiss_trust_ritual_card(shell)
        self.assertFalse(trust_ritual.show_trust_ritual_card(shell, identity))
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))

    def test_show_then_drain_then_show_does_not_double_render(self) -> None:
        # Simulates real run() ordering: refresh_state fires
        # first_readable_connect during the synchronous startup read
        # (stashing), drain fires after dpg.create_context(), then a
        # later refresh_from_controller fires first_readable_connect
        # again with the same identity. The per-session gate must
        # suppress the second render.
        shell = _make_shell_for_card()
        shell._dpg_context_ready = False
        identity = _identity()

        # Pre-DPG: stash.
        trust_ritual.show_trust_ritual_card(shell, identity)
        # DPG up: drain renders.
        shell._dpg_context_ready = True
        trust_ritual.drain_pending_trust_ritual(shell)
        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))
        trust_ritual.dismiss_trust_ritual_card(shell)

        # Post-DPG show with same identity: gated.
        self.assertFalse(trust_ritual.show_trust_ritual_card(shell, identity))
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))


class RunStartupSequenceIntegrationTests(unittest.TestCase):
    """End-to-end: the real ``AppShell.run()`` ordering renders exactly once.

    Mimics the startup sequence:
      1. (synthetic) first_readable_connect fires before DPG is ready.
      2. ``_dpg_context_ready = True`` is flipped.
      3. ``drain_pending_trust_ritual`` is called (the new wiring).
      4. The post-DPG ``refresh_from_controller`` fires
         ``_maybe_capture_first_readable_connect`` again with the same
         identity.

    Expected: one visible card, the per-session gate suppresses the
    redundant post-DPG attempt, ``rendered_keys`` has exactly one entry.
    """

    def setUp(self) -> None:
        set_locale("en")
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass

    def tearDown(self) -> None:
        dpg.destroy_context()
        set_locale("en")

    def test_startup_sequence_produces_one_render(self) -> None:
        shell = _make_shell_for_card()
        shell.device_service.state.product_name = "ZD Ultimate Legend"
        shell.device_service.state.firmware_version = "1.18"

        # Step 1: pre-DPG first_readable_connect. The shell's helper
        # already wraps show in try/except, so even with the new
        # deferral the gate doesn't advance and pending_render is set.
        shell._dpg_context_ready = False
        shell._maybe_capture_first_readable_connect()
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))
        self.assertIsNotNone(shell._trust_ritual_state.pending_render)

        # Step 2 + 3: DPG ready, drain.
        shell._dpg_context_ready = True
        trust_ritual.drain_pending_trust_ritual(shell)
        self.assertTrue(trust_ritual.trust_ritual_is_visible(shell))
        self.assertEqual(len(shell._trust_ritual_state.rendered_keys), 1)

        # Step 4: post-DPG refresh_from_controller fires the helper
        # again — the RP-set + trust-ritual-set BOTH gate.
        trust_ritual.dismiss_trust_ritual_card(shell)
        shell._maybe_capture_first_readable_connect()
        self.assertFalse(trust_ritual.trust_ritual_is_visible(shell))
        self.assertEqual(len(shell._trust_ritual_state.rendered_keys), 1)


# ---------------------------------------------------------------------------
# Locale parity + forbidden-phrase extension
# ---------------------------------------------------------------------------


def _load_locale(name: str) -> dict[str, str]:
    return json.loads((_LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class TrustRitualI18nKeysTests(unittest.TestCase):
    """Both locales define every spec'd trust_ritual.* key."""

    def test_en_has_all_keys(self) -> None:
        data = _load_locale("en")
        missing = [k for k in _TRUST_RITUAL_KEYS if k not in data]
        self.assertEqual(missing, [])

    def test_zh_cn_has_all_keys(self) -> None:
        data = _load_locale("zh-CN")
        missing = [k for k in _TRUST_RITUAL_KEYS if k not in data]
        self.assertEqual(missing, [])

    def test_locales_have_identical_trust_ritual_keyset(self) -> None:
        en = {k for k in _load_locale("en") if k.startswith("trust_ritual.")}
        zh = {k for k in _load_locale("zh-CN") if k.startswith("trust_ritual.")}
        self.assertEqual(en, zh)

    def test_zh_cn_strings_are_actually_translated(self) -> None:
        # Cheap-but-useful sanity: the zh-CN value must differ from the
        # English value (catches forgotten translations / accidental
        # copy-paste of the en string into zh-CN).
        en = _load_locale("en")
        zh = _load_locale("zh-CN")
        for key in _TRUST_RITUAL_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(en[key], zh[key])


# Forbidden-token blocklist — same tokens that test_restore_point_forbidden_phrases
# enforces against the restore-points feature copy. Trust ritual is a
# different feature, but the wrapper's overall trust posture mandates
# the same vocabulary discipline anywhere we make a promise to the user.
_FORBIDDEN_TOKENS = (
    "factory_backup",
    "factory_restore",
    "factory_image",
    "full_backup",
    "complete_backup",
    "clone",
    "calibration_backup",
    "firmware_backup",
    "guaranteed_rollback",
    "factory",
    "backup",
)


class TrustRitualForbiddenPhrasesTests(unittest.TestCase):
    """No trust_ritual.* string may contain a blocked token."""

    def _assert_no_forbidden(self, locale_name: str) -> None:
        data = _load_locale(locale_name)
        for key, value in data.items():
            if not key.startswith("trust_ritual."):
                continue
            lowered = value.lower()
            for token in _FORBIDDEN_TOKENS:
                with self.subTest(locale=locale_name, key=key, token=token):
                    self.assertNotIn(
                        token,
                        lowered,
                        msg=(
                            f"Locale {locale_name} key {key!r} value "
                            f"{value!r} contains forbidden token {token!r}"
                        ),
                    )

    def test_en_trust_ritual_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden("en")

    def test_zh_cn_trust_ritual_strings_have_no_forbidden_tokens(self) -> None:
        self._assert_no_forbidden("zh-CN")


if __name__ == "__main__":
    unittest.main()
