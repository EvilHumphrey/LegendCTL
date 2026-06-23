"""Tests for the Diagnostics screen's dev-panel gating (UX cleanup)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import make_shell
from zd_app import i18n
from zd_app.models import AppSettings
from zd_app.ui import typography
from zd_app.ui.screens import diagnostics


# Tag rendered only inside the Developer tab (the Raw-HID card); used to verify
# the developer_panels_visible gate. The Developer tab only mounts when the
# toggle is on.
_DEV_ONLY_TAGS = (
    "diag_raw_hid_enabled",
)
# Tags rendered regardless of toggle state.
_ALWAYS_PRESENT_TAGS = (
    "diag_health_summary",
    "diag_event_log",
)


def _build_in_fresh_context(shell) -> None:
    with dpg.window():
        with dpg.child_window(tag="content_region"):
            pass
    diagnostics.build(shell, "content_region")


class DiagnosticsDevPanelGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_dev_panels_hidden_when_toggle_off(self) -> None:
        # Default AppSettings has developer_panels_visible=False
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)

            for tag in _DEV_ONLY_TAGS:
                self.assertFalse(
                    dpg.does_item_exist(tag),
                    f"Dev-only tag {tag!r} should be hidden when toggle is off",
                )
            # Always-present sections still render.
            for tag in _ALWAYS_PRESENT_TAGS:
                self.assertTrue(
                    dpg.does_item_exist(tag),
                    f"Always-present tag {tag!r} should still render",
                )
        finally:
            dpg.destroy_context()

    def test_dev_panels_visible_when_toggle_on(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)

            for tag in _DEV_ONLY_TAGS:
                self.assertTrue(
                    dpg.does_item_exist(tag),
                    f"Dev-only tag {tag!r} should render when toggle is on",
                )
        finally:
            dpg.destroy_context()


class _DiagnosticsChildWindowRecorder:
    """Patches ``diagnostics.dpg.child_window`` to record kwargs without
    requiring a real DPG context. Used by ``DiagnosticsCardHeightTests``
    to check fixed heights against the audit floor without rendering."""

    class _CM:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def __init__(self) -> None:
        self.child_windows: list[dict] = []
        self._patches: list = []

    def __enter__(self) -> "_DiagnosticsChildWindowRecorder":
        def record_child_window(*_args, **kw):
            self.child_windows.append(kw)
            return self._CM()

        def passthrough(*_args, **_kw):
            return self._CM()

        # The diagnostics module reaches for many DPG symbols; replacing
        # them all individually is brittle. Patch the whole module-level
        # ``dpg`` proxy so calls succeed without a real context.
        fake_dpg = MagicMock()
        fake_dpg.child_window = record_child_window
        fake_dpg.group = passthrough
        fake_dpg.tree_node = passthrough
        fake_dpg.drawlist = passthrough
        # Items that return tags must echo something usable.
        fake_dpg.add_text = MagicMock(return_value="t")
        fake_dpg.add_button = MagicMock(return_value="b")
        fake_dpg.add_spacer = MagicMock(return_value="s")
        fake_dpg.add_separator = MagicMock(return_value="sep")
        fake_dpg.add_checkbox = MagicMock(return_value="cb")
        fake_dpg.add_progress_bar = MagicMock(return_value="pb")
        fake_dpg.draw_rectangle = MagicMock()
        fake_dpg.draw_circle = MagicMock()
        fake_dpg.does_item_exist = MagicMock(return_value=False)
        fake_dpg.get_frame_count = MagicMock(return_value=0)
        fake_dpg.set_frame_callback = MagicMock()
        fake_dpg.set_clipboard_text = MagicMock()
        fake_dpg.set_value = MagicMock()
        fake_dpg.bind_item_theme = MagicMock()
        cm = patch.object(diagnostics, "dpg", fake_dpg)
        cm.__enter__()
        self._patches.append(cm)
        # The typography helpers (screen_title/section_title/helper_text)
        # use their own module-level ``dpg``; patch it too so title rendering
        # routes through the fake instead of hitting a real (absent) context.
        cm_typo = patch.object(typography, "dpg", fake_dpg)
        cm_typo.__enter__()
        self._patches.append(cm_typo)
        return self

    def __exit__(self, *exc) -> None:
        for cm in reversed(self._patches):
            cm.__exit__(*exc)


class DiagnosticsCardHeightTests(unittest.TestCase):
    """Regressions for the 2026-05-26 DPG card-height audit.

    The "Actions" and "Calibration And Recovery" cards in the second
    horizontal row of Diagnostics (sized 280×246 / 400×246 pre-fix) had
    fixed heights well below the content they render. With the theme
    (``ItemSpacing=8`` + ``WindowPadding=16``, button rows ~25px, text
    rows ~13-15px including wrap), the audit measured:

    - **Actions** card needs ~349px: 1 label + 8 buttons + 1 wrap=260
      helper text (2 lines) + 9 ItemSpacings + 32 padding. Pre-fix value
      (246) clipped the last 3 buttons + their preceding helper.
    - **Calibration** card needs ~391px: 1 label + 2-line summary + 5
      mixed 1- and 2-line bullets + 2-line firmware_target_split + 4-line
      windows-support paragraph + 2 spacers + 11 ItemSpacings + padding.
      Pre-fix value (246) clipped the entire windows-support paragraph.

    Tests pin computed floors so future content edits cannot drop back
    below the safe range without surfacing the regression here.
    """

    _ACTIONS_FLOOR = 340           # 8 buttons + 2-line helper + padding
    _CALIBRATION_FLOOR = 390       # 5 bullets + 2 paragraphs + padding

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_actions_card_clears_minimum_height(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")
        # The Actions card is the first 280-wide sized child_window in
        # the second card row.
        actions_cards = [
            kw for kw in rec.child_windows
            if kw.get("width") == 280 and kw.get("height") is not None
        ]
        self.assertEqual(
            len(actions_cards), 1,
            f"Expected exactly one width=280 sized card (Actions); "
            f"got {[kw.get('height') for kw in actions_cards]}",
        )
        height = actions_cards[0]["height"]
        self.assertGreaterEqual(
            height, self._ACTIONS_FLOOR,
            f"Diagnostics 'Actions' card height={height} cannot fit its "
            f"8 buttons + helper text. Pre-fix value (246) clipped the "
            f"trailing Open Firmware / Open Stack / Clear Logs buttons.",
        )

    def test_calibration_card_clears_minimum_height(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")
        # The Calibration And Recovery card is the only 400-wide sized
        # child_window in the second card row.
        cal_cards = [
            kw for kw in rec.child_windows
            if kw.get("width") == 400 and kw.get("height") is not None
        ]
        self.assertEqual(
            len(cal_cards), 1,
            f"Expected exactly one width=400 sized card (Calibration); "
            f"got {[kw.get('height') for kw in cal_cards]}",
        )
        height = cal_cards[0]["height"]
        self.assertGreaterEqual(
            height, self._CALIBRATION_FLOOR,
            f"Diagnostics 'Calibration And Recovery' card height={height} "
            f"cannot fit its summary + 5 bullets + 2 paragraphs at "
            f"wrap=360. Pre-fix value (246) clipped the entire "
            f"windows-support paragraph at the bottom.",
        )


class DiagnosticsTrustCardTests(unittest.TestCase):
    """Regression for the DPG card-clip lane (2026-06-21).

    "What To Trust" used to be the 3rd, width=-1 (flex) card in the
    Actions/Calibration row, where its width collapsed to ~187px at the minimum
    window and its fixed wrap=780 prose overran the right edge (269px at 1480,
    302px at 1180 — tools/diag_dpg_card_clip.py). It now renders on its OWN
    full-width row and fits its content (auto_resize_y), so it can never be
    squeezed or clipped.
    """

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_trust_card_is_full_width_content_fit_not_a_fixed_column(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")
        trust = [
            kw for kw in rec.child_windows
            if kw.get("tag") == diagnostics.TRUST_CARD_TAG
        ]
        self.assertEqual(
            len(trust), 1,
            f"Expected exactly one What-To-Trust card; got {trust!r}",
        )
        kw = trust[0]
        # Full-width row (not a cramped fixed-width 3rd column).
        self.assertEqual(
            kw.get("width"), -1,
            "Trust card must span the full content width.",
        )
        # Content-fit, not a hand-measured fixed height that could clip.
        self.assertIsNone(
            kw.get("height"),
            f"Trust card must fit content, not pin a fixed height: {kw}",
        )
        self.assertTrue(
            kw.get("auto_resize_y"),
            "Trust card must use auto_resize_y (content-fit).",
        )
        self.assertFalse(
            kw.get("autosize_y", False),
            "Trust card must suppress the legacy fill flag.",
        )


class DiagnosticsPhase1RelocationTests(unittest.TestCase):
    """Phase-1 leaner-screen cleanups (2026-06-21):

    - Raw HID frame log is now gated behind ``developer_panels_visible`` (a
      research affordance, not an everyday diagnostic).
    - The manual Save-Restore-Point button moved to the Restore Points screen.
    - The Legacy-screens toggle moved to Preferences.
    - The Build-info card was deleted (About already shows version + commit).
    """

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_raw_hid_section_hidden_when_developer_toggle_off(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertFalse(dpg.does_item_exist("diag_raw_hid_enabled"))
            self.assertFalse(dpg.does_item_exist("diag_raw_hid_log"))
        finally:
            dpg.destroy_context()

    def test_raw_hid_section_visible_when_developer_toggle_on(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("diag_raw_hid_enabled"))
            self.assertTrue(dpg.does_item_exist("diag_raw_hid_log"))
        finally:
            dpg.destroy_context()

    def test_relocated_and_deleted_sections_absent_from_diagnostics(self) -> None:
        # Save-Restore-Point button (moved to Restore Points) and the legacy
        # toggle (moved to Preferences) no longer render on Diagnostics in
        # either toggle state.
        for dev in (False, True):
            with self.subTest(developer_panels_visible=dev):
                settings = AppSettings(developer_panels_visible=dev)
                shell = make_shell(settings_service=MagicMock(), settings=settings)
                dpg.create_context()
                try:
                    _build_in_fresh_context(shell)
                    self.assertFalse(dpg.does_item_exist("diag_restore_point_save_button"))
                    self.assertFalse(dpg.does_item_exist("diag_restore_point_status_text"))
                    self.assertFalse(dpg.does_item_exist("diag_show_legacy_screens"))
                finally:
                    dpg.destroy_context()


class DiagnosticsTabStructureTests(unittest.TestCase):
    """Phase-2 tab restructure (2026-06-21): the cards are distributed across a
    Status / Actions / Guidance / Developer tab bar (mirroring the Controller
    screen) so each view fits the window instead of one long page scroll."""

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _shell(self, **settings_kw):
        settings = AppSettings(**settings_kw)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        return shell

    def test_tab_bar_and_shipping_tabs_mount(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("diagnostics_tab_bar"))
            for tag in ("diag_tab_status", "diag_tab_actions", "diag_tab_guidance"):
                self.assertTrue(dpg.does_item_exist(tag), f"missing tab {tag}")
            self.assertFalse(dpg.does_item_exist("diag_tab_developer"))
        finally:
            dpg.destroy_context()

    def test_developer_tab_present_and_holds_dev_cards_when_dev_on(self) -> None:
        shell = self._shell(developer_panels_visible=True)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("diag_tab_developer"))
            # The Developer tab holds the Raw-HID card (the transport/replay
            # research panels are not part of this build).
            self.assertTrue(
                dpg.does_item_exist("diag_raw_hid_enabled"), "missing dev tag diag_raw_hid_enabled"
            )
        finally:
            dpg.destroy_context()

    def test_shipping_tab_content_tags_present(self) -> None:
        # Status (health + connection), Guidance (event log) tags render
        # regardless of which tab is active (all tab children exist in the
        # registry — the tick path addresses them by tag).
        shell = self._shell(developer_panels_visible=False)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("diag_health_summary"))
            self.assertTrue(dpg.does_item_exist("diag_connection_details"))
            self.assertTrue(dpg.does_item_exist(diagnostics.TRUST_CARD_TAG))
            self.assertTrue(dpg.does_item_exist("diag_event_log"))
            self.assertTrue(dpg.does_item_exist(diagnostics.OPEN_LIVE_VERIFY_BUTTON_TAG))
        finally:
            dpg.destroy_context()

    def test_stale_warning_pinned_above_tab_bar(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell.device_service.state.data_freshness = "stale"
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            root = (dpg.get_item_children("content_region", 1) or [None])[0]
            kids = dpg.get_item_children(root, 1) or []
            tab_bar_idx = next(
                i for i, k in enumerate(kids)
                if dpg.get_item_alias(k) == "diagnostics_tab_bar"
            )
            child_window_idxs = [
                i for i, k in enumerate(kids)
                if dpg.get_item_type(k).endswith("mvChildWindow")
            ]
            self.assertTrue(
                any(i < tab_bar_idx for i in child_window_idxs),
                "Stale warning must render as a direct sibling ABOVE the tab bar, "
                "never tucked behind a tab.",
            )
        finally:
            dpg.destroy_context()

    def test_no_card_directly_above_tab_bar_when_fresh(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell.device_service.state.data_freshness = "fresh"
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            root = (dpg.get_item_children("content_region", 1) or [None])[0]
            kids = dpg.get_item_children(root, 1) or []
            child_window_idxs = [
                i for i, k in enumerate(kids)
                if dpg.get_item_type(k).endswith("mvChildWindow")
            ]
            # Tab content cards are nested inside the tabs, not direct children
            # of the screen root; only the (absent) stale card would be.
            self.assertEqual(
                child_window_idxs, [],
                "No card should render directly under the screen root when fresh.",
            )
        finally:
            dpg.destroy_context()

    def test_active_tab_persists_across_rebuild(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell.diagnostics_active_tab = "guidance"
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            selected = dpg.get_value("diagnostics_tab_bar")
            if isinstance(selected, int):
                selected = dpg.get_item_alias(selected)
            self.assertEqual(selected, "diag_tab_guidance")
        finally:
            dpg.destroy_context()

    def test_persisted_developer_tab_skips_cleanly_when_dev_off(self) -> None:
        # User was on Developer, then turned dev panels off: the rebuild has no
        # diag_tab_developer to re-select. The does_item_exist guard must skip
        # without raising, leaving the default tab.
        shell = self._shell(developer_panels_visible=False)
        shell.diagnostics_active_tab = "developer"
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)  # must not raise
            self.assertFalse(dpg.does_item_exist("diag_tab_developer"))
        finally:
            dpg.destroy_context()


class DiagnosticsTabPersistenceHelpersTests(unittest.TestCase):
    """Pure-function + callback coverage for the active-tab persistence."""

    def test_tab_id_tag_roundtrip(self) -> None:
        for tab_id in diagnostics.DIAGNOSTICS_TAB_IDS:
            tag = diagnostics._diag_tab_id_to_tag(tab_id)
            self.assertEqual(tag, f"diag_tab_{tab_id}")
            self.assertEqual(diagnostics._diag_tab_tag_to_id(tag), tab_id)

    def test_unknown_id_and_tag_default_to_status(self) -> None:
        self.assertEqual(diagnostics._diag_tab_id_to_tag("bogus"), "diag_tab_status")
        self.assertEqual(diagnostics._diag_tab_tag_to_id("diag_tab_bogus"), "status")
        self.assertEqual(diagnostics._diag_tab_tag_to_id("not_a_tab_tag"), "status")

    def test_remember_active_tab_stores_id_on_shell(self) -> None:
        shell = MagicMock()
        diagnostics._remember_active_tab(shell, "diag_tab_actions")
        self.assertEqual(shell.diagnostics_active_tab, "actions")

    def test_tab_bar_callback_updates_active_tab(self) -> None:
        i18n.set_locale("en")
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            callback = dpg.get_item_callback("diagnostics_tab_bar")
            self.assertIsNotNone(callback)
            callback("diagnostics_tab_bar", "diag_tab_guidance", None)
            self.assertEqual(shell.diagnostics_active_tab, "guidance")
        finally:
            dpg.destroy_context()


class DiagnosticsDeveloperCardFitTests(unittest.TestCase):
    """Phase-2: Developer-tab cards fit their content (auto_resize_y) so they
    never grow an inner scrollbar; the old fixed dev-card heights are gone."""

    # Old fixed dev-card heights (pre-content-fit). No shipped dev card should
    # use these; the guard catches a regression back to a fixed height.
    _OLD_DEV_FIXED_HEIGHTS = {210, 160, 150, 220}

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_developer_cards_are_content_fit_not_fixed_height(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")
        offending = [
            kw for kw in rec.child_windows
            if kw.get("height") in self._OLD_DEV_FIXED_HEIGHTS
            and kw.get("width") not in (220, 320)  # Status-tab Health/Connection keep 220
        ]
        self.assertEqual(
            [kw.get("height") for kw in offending], [],
            "Developer cards must be content-fit (auto_resize_y), not the old "
            "fixed heights (210/160/150/220).",
        )
        fit_cards = [kw for kw in rec.child_windows if kw.get("auto_resize_y") is True]
        # What-To-Trust (Guidance tab) + Raw-HID (Developer tab) are content-fit
        # cards with dev ON.
        self.assertGreaterEqual(
            len(fit_cards), 2,
            f"Expected >=2 content-fit cards with dev ON; got {len(fit_cards)}.",
        )


if __name__ == "__main__":
    unittest.main()
