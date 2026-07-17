"""Tests for the Diagnostics screen's dev-panel gating (UX cleanup)."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app import i18n
from zd_app.models import AppSettings
from zd_app.services.model_fingerprint import InterfaceInventory, ModelFingerprint
from zd_app.ui import right_rail, trust_front_door
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
        self.text_items: list[dict] = []
        self.input_text_items: list[dict] = []
        self._patches: list = []

    def __enter__(self) -> "_DiagnosticsChildWindowRecorder":
        def record_child_window(*_args, **kw):
            self.child_windows.append(kw)
            return self._CM()

        def record_add_text(*args, **kw):
            self.text_items.append({"args": args, **kw})
            return kw.get("tag", "t")

        def record_add_input_text(*args, **kw):
            self.input_text_items.append({"args": args, **kw})
            return kw.get("tag", "input")

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
        fake_dpg.add_text = record_add_text
        fake_dpg.add_input_text = record_add_input_text
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
        cm_trust = patch.object(trust_front_door, "dpg", fake_dpg)
        cm_trust.__enter__()
        self._patches.append(cm_trust)
        cm_right_rail = patch.object(right_rail, "dpg", fake_dpg)
        cm_right_rail.__enter__()
        self._patches.append(cm_right_rail)
        cm_right_rail_wide = patch.object(right_rail, "is_wide", return_value=False)
        cm_right_rail_wide.__enter__()
        self._patches.append(cm_right_rail_wide)
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

    def test_connection_details_card_wraps_trust_evidence(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")

        connection_cards = [
            kw for kw in rec.child_windows
            if kw.get("width") == diagnostics._CONNECTION_DETAILS_CARD_WIDTH
        ]
        self.assertEqual(len(connection_cards), 1)
        self.assertGreater(
            connection_cards[0]["width"],
            320,
            "Connection Details must be wider than the old clipping card.",
        )

        details = [
            kw for kw in rec.text_items
            if kw.get("tag") == "diag_connection_details"
        ]
        self.assertEqual(len(details), 1)
        self.assertGreater(
            details[0].get("wrap", 0),
            0,
            "Connection Details evidence text must wrap inside the card.",
        )

    def test_compat_report_preview_height_is_reduced_to_avoid_scroll_trap(self) -> None:
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")

        previews = [
            kw for kw in rec.input_text_items
            if kw.get("tag") == diagnostics.COMPAT_REPORT_PREVIEW_TAG
        ]
        self.assertEqual(len(previews), 1)
        self.assertTrue(previews[0]["readonly"])
        self.assertTrue(previews[0]["multiline"])
        self.assertEqual(
            previews[0]["height"],
            diagnostics._COMPAT_REPORT_PREVIEW_HEIGHT,
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
            self.assertTrue(dpg.does_item_exist(diagnostics.TRUST_SELF_CHECK_CARD_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.TRUST_SELF_CHECK_COPY_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.COMPAT_REPORT_CARD_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.COMPAT_REPORT_COPY_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.SHARE_CARD_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.SHARE_CARD_SAVE_TAG))
            self.assertTrue(dpg.does_item_exist(diagnostics.SHARE_CARD_COPY_TAG))
            self.assertTrue(dpg.does_item_exist("diag_event_log"))
            self.assertTrue(dpg.does_item_exist(diagnostics.OPEN_LIVE_VERIFY_BUTTON_TAG))
            for link in trust_front_door.TRUST_FRONT_DOOR_LINKS:
                self.assertTrue(
                    dpg.does_item_exist(
                        trust_front_door.button_tag(
                            "diagnostics_status_trust_front_door",
                            link.target,
                        )
                    )
                )
        finally:
            dpg.destroy_context()

    def test_status_tab_uses_two_column_grid_on_wide_viewports(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell._viewport_client_width = lambda: 2560
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist("diagnostics_status_grid"))
            self.assertTrue(dpg.does_item_exist("diag_health_summary"))
            self.assertTrue(dpg.does_item_exist("diag_connection_details"))
        finally:
            dpg.destroy_context()

    def test_status_trust_front_door_links_route_to_guidance(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell.switch_screen = MagicMock()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            target = "self_check"
            callback = dpg.get_item_callback(
                trust_front_door.button_tag(
                    "diagnostics_status_trust_front_door",
                    target,
                )
            )
            callback("sender", None, None)
        finally:
            dpg.destroy_context()

        self.assertEqual(shell.diagnostics_active_tab, "guidance")
        self.assertEqual(
            getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR),
            target,
        )
        shell.switch_screen.assert_called_once_with("diagnostics")

    def test_status_trust_front_door_strip_keeps_single_row_structure(self) -> None:
        # Regression pin for the Home 2x2 wrap (visual review 2026-07-06): the
        # status strip is height-budgeted at 66px, so this call site must keep
        # the original single horizontal row — all four buttons as direct
        # children of the links group, no wrapped row subgroups — at any
        # viewport width.
        for viewport_width in (1480, 2560):
            with self.subTest(viewport_width=viewport_width):
                shell = self._shell(developer_panels_visible=False)
                shell._viewport_client_width = lambda width=viewport_width: width
                links_tag = "diagnostics_status_trust_front_door_links"
                dpg.create_context()
                try:
                    _build_in_fresh_context(shell)
                    self.assertTrue(dpg.does_item_exist(links_tag))
                    self.assertTrue(
                        dpg.get_item_configuration(links_tag)["horizontal"]
                    )
                    self.assertFalse(dpg.does_item_exist(f"{links_tag}_row_0"))
                    children = dpg.get_item_children(links_tag, 1) or []
                    self.assertEqual(
                        [dpg.get_item_alias(child) for child in children],
                        [
                            trust_front_door.button_tag(
                                "diagnostics_status_trust_front_door", link.target
                            )
                            for link in trust_front_door.TRUST_FRONT_DOOR_LINKS
                        ],
                    )
                finally:
                    dpg.destroy_context()

    def test_trust_self_check_display_caveat_is_consolidated(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)

            self.assertEqual(
                dpg.get_value(diagnostics.TRUST_SELF_CHECK_INTRO_TAG),
                i18n.t("trust_self_check.display_caveat"),
            )
            self.assertTrue(
                dpg.does_item_exist(diagnostics.TRUST_SELF_CHECK_SCOPE_DETAILS_TAG)
            )
            for index in range(5):
                self.assertFalse(
                    dpg.does_item_exist(
                        f"diagnostics_trust_self_check_boundary_{index}"
                    )
                )
                self.assertTrue(
                    dpg.does_item_exist(
                        f"diagnostics_trust_self_check_claim_{index}"
                    )
                )
                self.assertTrue(
                    dpg.does_item_exist(
                        f"diagnostics_trust_self_check_evidence_{index}"
                    )
                )
            extra_tag = "diagnostics_trust_self_check_boundary_extra_1"
            self.assertTrue(dpg.does_item_exist(extra_tag))
            self.assertIn(
                "app-footprint check, not a whole-PC or game-compatibility clearance",
                dpg.get_value(extra_tag),
            )
        finally:
            dpg.destroy_context()

    def test_trust_self_check_mounts_model_fingerprint_block_when_collected(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        shell.device_service.state.model_fingerprint = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            version_number=0x0124,
            product_string="ZD Ultimate Legend",
            manufacturer_string="ZD",
            usage_page=0xFF00,
            usage=0x0001,
            input_report_len=64,
            output_report_len=65,
            feature_report_len=17,
            button_caps_count=10,
            value_caps_count=6,
            interface_inventory=InterfaceInventory(count=3, mi_indices=(0, 1, 2)),
        )
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)

            self.assertTrue(
                dpg.does_item_exist(diagnostics.TRUST_SELF_CHECK_MODEL_FINGERPRINT_TAG)
            )
            self.assertIn(
                "Fingerprint digest",
                dpg.get_value("diagnostics_model_fingerprint_row_0"),
            )
            self.assertEqual(
                dpg.get_value("diagnostics_model_fingerprint_basis"),
                "Write validation basis: ZD Ultimate Legend (wired USB)",
            )
        finally:
            dpg.destroy_context()

    def test_trust_self_check_model_fingerprint_block_tracks_late_arrival(self) -> None:
        # G3: the fingerprint is collected asynchronously (~2.5s after connect),
        # so the Diagnostics trust card is first built with model_fingerprint=None
        # (block absent). A later rebuild that sees it must mount the block, and a
        # rebuild after it clears must drop it (symmetric).
        shell = self._shell(developer_panels_visible=False)
        fingerprint = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            interface_inventory=InterfaceInventory(count=1, mi_indices=(2,)),
        )
        tag = diagnostics.TRUST_SELF_CHECK_MODEL_FINGERPRINT_TAG

        # Built before the fingerprint arrives: block absent.
        shell.device_service.state.model_fingerprint = None
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertFalse(dpg.does_item_exist(tag))
        finally:
            dpg.destroy_context()

        # Fingerprint arrives; the rebuild mounts the block.
        shell.device_service.state.model_fingerprint = fingerprint
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist(tag))
        finally:
            dpg.destroy_context()

        # Symmetric: the fingerprint clears; the rebuild drops the block.
        shell.device_service.state.model_fingerprint = None
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertFalse(dpg.does_item_exist(tag))
        finally:
            dpg.destroy_context()

    def test_drivers_extra_boundary_is_visible_not_inside_scope_details(self) -> None:
        shell = self._shell(developer_panels_visible=False)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            extra_tag = "diagnostics_trust_self_check_boundary_extra_1"

            self.assertTrue(dpg.does_item_exist(extra_tag))
            self.assertIn(
                "app-footprint check, not a whole-PC or game-compatibility clearance",
                dpg.get_value(extra_tag),
            )
            parent = dpg.get_item_info(extra_tag)["parent"]
            self.assertNotEqual(
                dpg.get_item_alias(parent),
                diagnostics.TRUST_SELF_CHECK_SCOPE_DETAILS_TAG,
                "Drivers-specific qualifier must render inline with the row, "
                "not inside the default-collapsed Scope details node.",
            )
        finally:
            dpg.destroy_context()

    def test_front_door_focus_is_one_shot_and_targets_real_guidance_tags(self) -> None:
        # REVISION 5: card targets are child windows and must NOT receive
        # keyboard focus (focus_item's own ensure-visible scroll caused the
        # windowed overscroll); only the evidence_card BUTTON keeps it. The
        # consume stays one-shot for every target.
        card_type = "mvAppItemType::mvChildWindow"
        for target, expected_tag, focusable in (
            ("self_check", diagnostics.TRUST_SELF_CHECK_CARD_TAG, False),
            ("compat_report", diagnostics.COMPAT_REPORT_CARD_TAG, False),
            ("evidence_card", diagnostics.SHARE_CARD_COPY_TAG, True),
        ):
            with self.subTest(target=target):
                shell = self._shell(developer_panels_visible=False)
                setattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, target)
                fake_dpg = MagicMock()
                fake_dpg.does_item_exist.return_value = True
                fake_dpg.focus_item = MagicMock()
                fake_dpg.get_item_type.side_effect = lambda item, _f=focusable: (
                    "mvAppItemType::mvButton"
                    if (_f and item == diagnostics.SHARE_CARD_COPY_TAG)
                    else card_type
                    if item
                    in (
                        diagnostics.TRUST_SELF_CHECK_CARD_TAG,
                        diagnostics.COMPAT_REPORT_CARD_TAG,
                    )
                    else "mvAppItemType::mvGroup"
                )

                with patch.object(diagnostics, "dpg", fake_dpg):
                    diagnostics._consume_trust_front_door_focus(shell)

                self.assertIsNone(
                    getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR)
                )
                if focusable:
                    fake_dpg.focus_item.assert_called_once_with(expected_tag)
                else:
                    fake_dpg.focus_item.assert_not_called()

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
            and kw.get("width") not in (
                220,
                diagnostics._CONNECTION_DETAILS_CARD_WIDTH,
            )
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


class DiagnosticsTrustMatrixTests(unittest.TestCase):
    """Provenance ("What we know right now") matrix: placement, the live
    build-then-arrive refresh through the real refresh_shell hook (both
    directions), the never-upgrade guard, and the 4th front-door link."""

    _UNIT_A = "unit-a-identity"

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _shell(self):
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        shell.draft_to_slot_write_transport = MagicMock()
        shell.last_draft_to_slot_write_result = None
        # settings_service is a MagicMock; pin the skipped-fields signal to a
        # real int so the gatherer's isinstance guard reads a clean 0.
        shell.settings_service.last_read_skipped_fields = 0
        return shell

    def _mark_connected_and_read(self, shell) -> None:
        state = shell.device_service.state
        state.connection_state = "connected"
        state.stable_identifier = self._UNIT_A
        state.data_freshness = "fresh"
        state.firmware_version = "1.18"
        # Firmware's only real source is the official ZD app UI scrape (not a
        # device read); active profile here comes from a verified protocol
        # switch. The chips must differ: firmware -> "From the official app",
        # profile -> "Verified from device".
        state.summary_sources["firmware"] = "official_app_ui"
        state.summary_sources["active_profile"] = "protocol"
        # A genuine protocol switch verified for THIS connection — the flag the
        # matrix's "Verified from device" profile chip now requires (a sticky
        # "protocol" source alone no longer earns it; see the disconnect
        # downgrade in device_service.refresh_state).
        state.active_profile_protocol_verified_this_connection = True
        state.model_fingerprint = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            interface_inventory=InterfaceInventory(count=1, mi_indices=(2,)),
        )
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        shell.last_snapshot_identity = self._UNIT_A

    def _label(self, index: int) -> str:
        return dpg.get_value(diagnostics._trust_matrix_label_tag(index))

    def test_card_and_row_tags_mount_below_self_check(self) -> None:
        shell = self._shell()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertTrue(dpg.does_item_exist(diagnostics.TRUST_MATRIX_CARD_TAG))
            for index in range(6):
                self.assertTrue(
                    dpg.does_item_exist(diagnostics._trust_matrix_claim_tag(index))
                )
                self.assertTrue(
                    dpg.does_item_exist(diagnostics._trust_matrix_label_tag(index))
                )
                self.assertTrue(
                    dpg.does_item_exist(diagnostics._trust_matrix_why_tag(index))
                )
        finally:
            dpg.destroy_context()

    def test_unknown_at_build_before_any_device(self) -> None:
        shell = self._shell()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            # Rows 0-4 (identity/firmware/profile/settings/fingerprint) unknown.
            for index in range(5):
                self.assertEqual(
                    self._label(index),
                    i18n.t("trust_matrix.label.unknown"),
                    f"row {index} should be unknown before any device",
                )
            # Row 5 (applied) is a POLICY row, not an evidence row: it derives
            # from no signal, so at REAL render time — with no device ever
            # connected — its chip must read "Verification policy" and must not
            # be ANY evidence chip. It used to render "Verified by read-back" in
            # the good/green color right here, in a matrix whose other five rows
            # correctly said they knew nothing.
            self.assertEqual(self._label(5), i18n.t("trust_matrix.label.policy"))
            for evidence in ("verified", "inferred", "unknown"):
                self.assertNotEqual(
                    self._label(5),
                    i18n.t(f"trust_matrix.label.{evidence}"),
                    f"policy row must not wear the {evidence} chip",
                )
        finally:
            dpg.destroy_context()

    def test_read_arrival_flips_rows_to_verified_through_refresh_shell(self) -> None:
        # Build FIRST (nothing read), THEN the read/fingerprint arrive and the
        # real refresh_shell hook must flip the labels live — no rebuild. The
        # chips are SOURCE-AWARE: identity/settings/fingerprint verify from the
        # live device, active profile verifies from a protocol switch, but
        # firmware's only source is the official-app scrape — so it must show
        # the "From the official app" chip, NOT "Verified from device".
        shell = self._shell()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(self._label(0), i18n.t("trust_matrix.label.unknown"))

            self._mark_connected_and_read(shell)
            shell.refresh_shell()

            for index in (0, 2, 3, 4):
                self.assertEqual(
                    self._label(index),
                    i18n.t("trust_matrix.label.verified"),
                    f"row {index} should be verified after read arrival",
                )
            # Firmware (row 1): official-app source, never "Verified from device".
            self.assertEqual(
                self._label(1), i18n.t("trust_matrix.label.official_app")
            )
            self.assertNotEqual(
                self._label(1), i18n.t("trust_matrix.label.verified")
            )
        finally:
            dpg.destroy_context()

    def test_firmware_source_flip_after_build_updates_chip_live(self) -> None:
        # Ordering guard: the in-place refresh re-derives rows each tick, so a
        # summary_source that changes AFTER the card is built must move the chip.
        # Start with an official-app firmware value, then simulate a (future)
        # protocol firmware read landing and confirm the chip upgrades live.
        shell = self._shell()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self._mark_connected_and_read(shell)
            shell.refresh_shell()
            self.assertEqual(
                self._label(1), i18n.t("trust_matrix.label.official_app")
            )

            shell.device_service.state.summary_sources["firmware"] = "protocol"
            shell.refresh_shell()
            self.assertEqual(self._label(1), i18n.t("trust_matrix.label.verified"))
        finally:
            dpg.destroy_context()

    def test_disconnect_downgrades_verified_rows_bidirectionally(self) -> None:
        # Built verified, then a disconnect must demote rows back down — the
        # honesty direction the setup drawer never needed (its flags are one-way).
        shell = self._shell()
        self._mark_connected_and_read(shell)
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(self._label(0), i18n.t("trust_matrix.label.verified"))

            # Disconnect: identity/values retained, live unit gone, fingerprint
            # cleared (device_service clears it off the ZD).
            state = shell.device_service.state
            state.connection_state = "no_device"
            state.data_freshness = "stale"
            state.stable_identifier = "unknown"
            state.model_fingerprint = None
            shell.refresh_shell()

            self.assertEqual(self._label(0), i18n.t("trust_matrix.label.inferred"))
            self.assertEqual(self._label(1), i18n.t("trust_matrix.label.inferred"))
            self.assertEqual(self._label(2), i18n.t("trust_matrix.label.inferred"))
            self.assertEqual(self._label(3), i18n.t("trust_matrix.label.inferred"))
            self.assertEqual(self._label(4), i18n.t("trust_matrix.label.unknown"))
        finally:
            dpg.destroy_context()

    def test_retained_settings_never_shown_as_verified(self) -> None:
        # Never-upgrade guard at the UI seam: a retained snapshot with no live
        # unit must render inferred, never "Verified from device".
        shell = self._shell()
        shell.device_service.state.connection_state = "no_device"
        shell.device_service.state.data_freshness = "stale"
        shell.last_controller_snapshot = empty_snapshot()
        shell.last_snapshot_ts = 1.0
        shell.last_snapshot_identity = self._UNIT_A
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self.assertEqual(self._label(3), i18n.t("trust_matrix.label.inferred"))
            self.assertNotEqual(self._label(3), i18n.t("trust_matrix.label.verified"))
        finally:
            dpg.destroy_context()

    def test_partial_read_shows_count_qualifier_live(self) -> None:
        shell = self._shell()
        dpg.create_context()
        try:
            _build_in_fresh_context(shell)
            self._mark_connected_and_read(shell)
            shell.settings_service.last_read_skipped_fields = 4
            shell.refresh_shell()

            qualifier_tag = diagnostics._trust_matrix_qualifier_tag(3)
            self.assertEqual(self._label(3), i18n.t("trust_matrix.label.verified"))
            self.assertTrue(dpg.is_item_shown(qualifier_tag))
            self.assertIn("4", dpg.get_value(qualifier_tag))
        finally:
            dpg.destroy_context()

    def test_front_door_fourth_link_targets_matrix_card(self) -> None:
        # The appended link reaches the matrix card, and the focus map resolves.
        targets = {link.target for link in trust_front_door.TRUST_FRONT_DOOR_LINKS}
        self.assertIn("trust_matrix", targets)
        self.assertEqual(
            diagnostics._TRUST_FRONT_DOOR_FOCUS_TARGETS["trust_matrix"],
            diagnostics.TRUST_MATRIX_CARD_TAG,
        )

    def test_front_door_matrix_consume_is_one_shot_and_skips_card_focus(self) -> None:
        # REVISION 5: the matrix card is a child_window, so the consume must
        # NOT keyboard-focus it (focus_item's own ensure-visible scroll caused
        # the windowed overscroll); the anchor scroll is the whole mechanism.
        # The focus attr is still consumed exactly once.
        shell = self._shell()
        setattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, "trust_matrix")
        fake_dpg = MagicMock()
        fake_dpg.does_item_exist.return_value = True
        fake_dpg.focus_item = MagicMock()
        fake_dpg.get_item_type.side_effect = lambda item: (
            "mvAppItemType::mvChildWindow"
            if item == diagnostics.TRUST_MATRIX_CARD_TAG
            else "mvAppItemType::mvGroup"
        )
        with patch.object(diagnostics, "dpg", fake_dpg):
            diagnostics._consume_trust_front_door_focus(shell)
        self.assertIsNone(
            getattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR)
        )
        fake_dpg.focus_item.assert_not_called()


class DiagnosticsFrontDoorScrollTests(unittest.TestCase):
    """The front-door consume must SCROLL the target card into view.

    REVISION 5 lessons enforced by these fakes (mock-validated geometry was
    hardware-falsified TWICE — container identity, then coordinate frames):

    - the parent chain is the REAL probe-verified rail nesting, with the root
      at zero scroll range and the range on the inner work column;
    - ``get_item_rect_min`` RAISES KeyError for every item, because real
      child_window items expose no rect_min (the REVISION-4 math silently
      never ran) — any future rect-based math fails these tests loudly;
    - offsets come from ``get_item_pos`` (window-relative layout positions,
      probe-verified), re-based across intervening child windows;
    - card targets are typed as child windows, so focus_item must NOT fire
      for them (its own ensure-visible scroll caused the windowed
      overscroll); the evidence_card BUTTON keeps focus.

    The shell is a real AppShell whose unarmed _defer_ui_call seam runs
    attempts inline."""

    _WORK = "diagnostics_work_column"
    _ROOT = "diagnostics_root"
    _SHARE = diagnostics.SHARE_CARD_TAG
    _CHILD_WINDOW = "mvAppItemType::mvChildWindow"

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _shell(self, target: str):
        settings = AppSettings(developer_panels_visible=False)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        setattr(shell, trust_front_door.TRUST_FRONT_DOOR_FOCUS_ATTR, target)
        return shell

    def _fake_dpg(
        self,
        *,
        target_tag: str,
        target_is_button: bool = False,
        target_pos_y: float = 995.0,
        work_scroll: float = 0.0,
        work_scroll_max: float = 1600.0,
        root_scroll_max: float = 0.0,
        share_card_pos_y: float = 1650.0,
    ) -> MagicMock:
        fake = MagicMock()
        fake.does_item_exist.return_value = True
        # REAL rail-layout ancestor chain (live-DPG probe 2026-07-06):
        # card -> guidance tab -> tab bar -> work column (child_window) ->
        # wide group -> root (child_window) -> content_region (child_window)
        # -> window. A BUTTON target additionally nests inside the share card
        # (child_window) via a horizontal group, exercising the pos re-basing
        # across an intermediate window.
        if target_is_button:
            parents = {
                target_tag: "share_card_button_row",
                "share_card_button_row": self._SHARE,
                self._SHARE: "diag_tab_guidance",
            }
        else:
            parents = {target_tag: "diag_tab_guidance"}
        parents.update(
            {
                "diag_tab_guidance": "diagnostics_tab_bar",
                "diagnostics_tab_bar": self._WORK,
                self._WORK: "diagnostics_root_wide_layout",
                "diagnostics_root_wide_layout": self._ROOT,
                self._ROOT: "content_region",
                "content_region": "primary_window",
                "primary_window": 0,
            }
        )
        types = {
            target_tag: (
                "mvAppItemType::mvButton" if target_is_button else self._CHILD_WINDOW
            ),
            "share_card_button_row": "mvAppItemType::mvGroup",
            self._SHARE: self._CHILD_WINDOW,
            "diag_tab_guidance": "mvAppItemType::mvTab",
            "diagnostics_tab_bar": "mvAppItemType::mvTabBar",
            self._WORK: self._CHILD_WINDOW,
            "diagnostics_root_wide_layout": "mvAppItemType::mvGroup",
            self._ROOT: self._CHILD_WINDOW,
            "content_region": self._CHILD_WINDOW,
            "primary_window": "mvAppItemType::mvWindowAppItem",
        }
        scroll_max = {
            self._WORK: work_scroll_max,
            self._ROOT: root_scroll_max,
            "content_region": 0.0,
            self._SHARE: 0.0,
        }
        # Window-relative layout positions (get_item_pos): a button's pos is
        # relative to the SHARE CARD (its containing window); a card's pos and
        # the share card's pos are relative to the WORK COLUMN.
        pos = {
            target_tag: target_pos_y,
            self._SHARE: share_card_pos_y,
            self._WORK: 66.0,
            self._ROOT: 0.0,
        }
        fake.get_item_parent.side_effect = lambda item: parents.get(item, 0)
        fake.get_item_type.side_effect = lambda item: types.get(
            item, "mvAppItemType::mvGroup"
        )
        fake.get_y_scroll_max.side_effect = lambda item: scroll_max.get(item, 0.0)
        fake.get_y_scroll.side_effect = (
            lambda item: work_scroll if item == self._WORK else 0.0
        )
        fake.get_item_pos.side_effect = lambda item: (0.0, pos.get(item, 0.0))

        def raise_rect_min(item):
            # Real child_window state has NO rect_min; the REVISION-4 rect
            # math died on this silently. Raise for EVERY item so any future
            # rect-based anchor math fails these tests loudly.
            raise KeyError("rect_min")

        fake.get_item_rect_min.side_effect = raise_rect_min
        return fake

    def test_walk_scrolls_inner_container_when_root_reports_zero_range(self) -> None:
        # The hardware-falsifying configuration: root y_scroll_max=0, work
        # column scrollable. The walk must find and scroll the WORK COLUMN
        # using the card's window-relative pos, and NOT focus the card
        # (child_window focus issues its own overscrolling ensure-visible).
        shell = self._shell("trust_matrix")
        fake = self._fake_dpg(
            target_tag=diagnostics.TRUST_MATRIX_CARD_TAG,
            target_pos_y=995.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)

        expected = 995.0 - diagnostics._TRUST_FRONT_DOOR_SCROLL_MARGIN
        self.assertGreater(expected, 0.0)
        fake.set_y_scroll.assert_called_once_with(self._WORK, expected)
        fake.focus_item.assert_not_called()

    def test_generic_path_offset_is_absolute_regardless_of_current_scroll(self) -> None:
        # compat_report proves the walk is generic, and the pos-derived offset
        # is ABSOLUTE content space — a pre-existing scroll must not shift it
        # (the rect math folded current scroll in; pos math must not).
        shell = self._shell("compat_report")
        fake = self._fake_dpg(
            target_tag=diagnostics.COMPAT_REPORT_CARD_TAG,
            target_pos_y=700.0,
            work_scroll=250.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)

        expected = 700.0 - diagnostics._TRUST_FRONT_DOOR_SCROLL_MARGIN
        fake.set_y_scroll.assert_called_once_with(self._WORK, expected)
        fake.focus_item.assert_not_called()

    def test_button_target_rebases_across_share_card_and_keeps_focus(self) -> None:
        # evidence_card points at a BUTTON nested inside the share card: its
        # pos is share-card-relative, so the walk must add the share card's
        # own work-column-relative pos (window re-basing), and the button —
        # an actionable control, not a card — KEEPS keyboard focus.
        shell = self._shell("evidence_card")
        fake = self._fake_dpg(
            target_tag=diagnostics.SHARE_CARD_COPY_TAG,
            target_is_button=True,
            target_pos_y=90.0,
            share_card_pos_y=1650.0,
            work_scroll_max=2000.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)

        expected = (90.0 + 1650.0) - diagnostics._TRUST_FRONT_DOOR_SCROLL_MARGIN
        fake.set_y_scroll.assert_called_once_with(self._WORK, expected)
        fake.focus_item.assert_called_once_with(diagnostics.SHARE_CARD_COPY_TAG)

    def test_unmeasured_pos_retries_then_falls_back_on_final_attempt(self) -> None:
        # A scrollable ancestor exists but layout positions are zeroed:
        # non-final attempts fail (re-queued inline through the unarmed seam)
        # and the FINAL attempt applies the proportional last resort to the
        # found container — never a zero-pos "measurement". No focus: card.
        shell = self._shell("trust_matrix")
        fake = self._fake_dpg(
            target_tag=diagnostics.TRUST_MATRIX_CARD_TAG,
            target_pos_y=0.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)

        expected = 1600.0 * diagnostics._TRUST_FRONT_DOOR_FALLBACK_SCROLL_FRACTIONS[
            "trust_matrix"
        ]
        self.assertGreater(expected, 0.0)
        fake.set_y_scroll.assert_called_once_with(self._WORK, expected)
        fake.focus_item.assert_not_called()

    def test_nothing_scrollable_requeues_bounded_through_defer_seam(self) -> None:
        # No ancestor advertises a scroll range: the anchor re-queues itself
        # through shell._defer_ui_call (a later drain pass when armed),
        # executes on subsequent drains, and stops at the attempt cap —
        # never scrolling, never raising, never focusing a card target.
        shell = self._shell("trust_matrix")
        queued: list = []
        shell._defer_ui_call = queued.append
        fake = self._fake_dpg(
            target_tag=diagnostics.TRUST_MATRIX_CARD_TAG,
            work_scroll_max=0.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)
            # The consume queues attempt 1; nothing ran inline.
            self.assertEqual(len(queued), 1)
            executed = 0
            drain_cap = diagnostics._TRUST_FRONT_DOOR_ANCHOR_MAX_ATTEMPTS + 5
            while queued and executed < drain_cap:
                queued.pop(0)()
                executed += 1

        self.assertEqual(
            executed, diagnostics._TRUST_FRONT_DOOR_ANCHOR_MAX_ATTEMPTS
        )
        fake.set_y_scroll.assert_not_called()
        fake.focus_item.assert_not_called()

    def test_no_scroll_when_content_fits(self) -> None:
        # All ranges zero and inline execution: the bounded attempts exhaust
        # synchronously and the last-resort proportional scroll no-ops on a
        # zero-range container. Card target -> no focus either.
        shell = self._shell("trust_matrix")
        fake = self._fake_dpg(
            target_tag=diagnostics.TRUST_MATRIX_CARD_TAG,
            work_scroll_max=0.0,
        )
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)

        fake.set_y_scroll.assert_not_called()
        fake.focus_item.assert_not_called()

    def test_missing_items_never_raise_or_scroll(self) -> None:
        shell = self._shell("trust_matrix")
        fake = MagicMock()
        fake.does_item_exist.return_value = False
        with patch.object(diagnostics, "dpg", fake):
            diagnostics._consume_trust_front_door_focus(shell)  # must not raise

        fake.set_y_scroll.assert_not_called()
        fake.focus_item.assert_not_called()


class DiagnosticsRailSafeWrapTests(unittest.TestCase):
    """Every text block on Diagnostics must wrap inside the rail work column.

    The rail/wide layout fixes the work column at right_rail.WORK_COLUMN_WIDTH
    (1040px, ~1008px inner after WindowPadding), but the Developer tab's
    debug/evidence paragraphs (wrap 1200/1180), the pinned stale banner (1040)
    and the event log (1200) were tuned for the WINDOWED content region and
    clipped at the right edge under the rail (2026-07-06 visual review,
    57_diagnostics_developer_maximized.png). Pin: build the FULL screen (dev
    tab + stale banner mounted) and assert no recorded text wrap exceeds the
    rail inner width — the codebase's 840 full-width measure satisfies this
    with margin.
    """

    _RAIL_INNER_WIDTH = right_rail.WORK_COLUMN_WIDTH - 32  # WindowPadding 16x2

    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def test_all_text_wraps_fit_the_rail_work_column(self) -> None:
        settings = AppSettings(developer_panels_visible=True)
        shell = make_shell(settings_service=MagicMock(), settings=settings)
        shell.draft_to_slot_write_transport = MagicMock()
        shell.last_draft_to_slot_write_result = None
        shell.settings_service.last_read_skipped_fields = 0
        # Mount the conditional stale banner too.
        shell.device_service.state.data_freshness = "stale"

        with _DiagnosticsChildWindowRecorder() as rec:
            diagnostics.build(shell, "content_region")

        offenders = [
            item
            for item in rec.text_items
            if isinstance(item.get("wrap"), (int, float))
            and item["wrap"] > self._RAIL_INNER_WIDTH
        ]
        self.assertEqual(
            offenders,
            [],
            "text blocks wrap wider than the rail work column's inner width "
            f"({self._RAIL_INNER_WIDTH}px) and will clip in the maximized "
            "layout",
        )


class DiagnosticsFrontDoorScrollIsolatedTests(unittest.TestCase):
    """Real-render gate for the anchor geometry AND timing (REVISION 5 + 6):
    mock-validated behavior was hardware-falsified three times, so the
    isolated child builds the REAL Diagnostics screen in a REAL viewport per
    shape — including the operator's actual maximized geometry (2576x1408) —
    and gates the landing position ([0, 40]px of the discovered container's
    visible top) plus the drain-from-frame-0 timing that exposed the attempt
    budget. One subprocess per method — a second DPG context in one process
    hits the known teardown segfault."""

    _METHODS = (
        "test_windowed_anchor_lands_matrix_card_at_container_top",
        "test_rail_anchor_lands_matrix_card_at_container_top",
        "test_rail_maximized_anchor_lands_matrix_card_at_container_top",
        "test_rail_real_timing_anchor_scrolls",
        "test_rail_maximized_real_timing_anchor_scrolls",
    )

    def test_isolated_real_render_anchor_geometry(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for method in self._METHODS:
            with self.subTest(method=method):
                test_id = (
                    "tests.isolated_diagnostics_frontdoor_scroll."
                    f"IsolatedDiagnosticsFrontDoorScrollTest.{method}"
                )
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "unittest", test_id],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except subprocess.TimeoutExpired as exc:
                    output_parts: list[str] = []
                    for part in (exc.stdout, exc.stderr):
                        if isinstance(part, bytes):
                            output_parts.append(part.decode(errors="replace"))
                        elif part:
                            output_parts.append(part)
                    self.fail(
                        "Isolated front-door scroll child hung (native render "
                        "hang class).\n\nChild output before timeout:\n"
                        + "\n".join(output_parts)
                    )

                output = "\n".join(
                    part for part in (result.stdout, result.stderr) if part
                )
                if any(line.strip() == "OK" for line in output.splitlines()):
                    continue
                self.fail(
                    "Isolated front-door scroll test did not report unittest OK.\n"
                    f"Return code: {result.returncode}\n"
                    f"Command: {sys.executable} -m unittest {test_id}\n\n"
                    f"Child output:\n{output}"
                )


if __name__ == "__main__":
    unittest.main()
