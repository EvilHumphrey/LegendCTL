"""DPG screen-build tests for the Device vs Profile screen.

Patches the ``dpg`` symbols on the screen module so ``build()`` runs without a
real DPG context. Because the screen module and the shared ``components`` /
``typography`` modules all bind the *same* ``dearpygui.dearpygui`` object as
``dpg``, patching the screen module's ``dpg`` attributes also covers the table
primitives those helpers call — without the table/table_row/add_table_column
stubs the real, context-less table calls would segfault (the ``components``
recorder trap noted in ``test_screens_controller.py``).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

from zd_app.models import WrapperProfile
from zd_app.services.settings_service import (
    ControllerSnapshot,
    PollingRate,
    SensitivityAnchor,
)
from zd_app.ui.screens import device_vs_profile as screen


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


def _snap(**kw) -> ControllerSnapshot:
    base = dict(
        polling_rate=None,
        vibration=None,
        deadzones=None,
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=None,
        sensitivity_right=None,
        trigger_left=None,
        trigger_right=None,
        button_bindings={},
        lighting_zones={},
    )
    base.update(kw)
    return ControllerSnapshot(**base)


class _FakeStore:
    """A minimal WrapperProfileStore double (no file IO / codec round-trip)."""

    def __init__(self, profiles: dict) -> None:
        # profiles: name -> snapshot
        self._profiles = profiles

    def list_profiles(self):
        return (
            [WrapperProfile(name=n, snapshot=s) for n, s in self._profiles.items()],
            [],
        )

    def load(self, name: str) -> WrapperProfile:
        return WrapperProfile(name=name, snapshot=self._profiles[name])


class _FakeRPService:
    """A fake RestorePointService exposing only the provenance read."""

    def __init__(self, snapshot, read_success, read_errors, *, boom: bool = False):
        self._triple = (snapshot, read_success, read_errors)
        self._boom = boom

    def read_current_state_with_provenance(self):
        if self._boom:
            raise TimeoutError("HID read timed out")
        return self._triple


def _shell(
    store,
    *,
    connected: bool = False,
    rp_service: Optional[_FakeRPService] = None,
    screen_state: Optional[screen.DeviceVsProfileScreenState] = None,
    last_snapshot=None,
) -> SimpleNamespace:
    device_service = SimpleNamespace(
        state=SimpleNamespace(
            connection_state="connected" if connected else "no_device"
        )
    )
    return SimpleNamespace(
        wrapper_profile_store=store,
        device_service=device_service,
        restore_point_service=rp_service,
        last_controller_snapshot=last_snapshot,
        device_vs_profile_screen_state=screen_state,
        COLORS={
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "warn": (3, 3, 3, 3),
            "bad": (4, 4, 4, 4),
            "good": (5, 5, 5, 5),
            "text": (6, 6, 6, 6),
        },
        rebuild_current_screen=MagicMock(),
    )


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _FakeGroup(*_args, **_kw):
    return _FakeContextManager()


class _PatchedScreen:
    """Context manager that captures DPG calls + button/combo/checkbox kwargs."""

    def __init__(self, shell) -> None:
        self.shell = shell
        self.values: dict[str, object] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.buttons: list[dict] = []
        self.combos: list[dict] = []
        self.checkboxes: list[dict] = []
        self.tables: list[dict] = []
        self.table_rows: list[dict] = []
        self.columns: list[dict] = []
        self._cm = None

    def __enter__(self) -> "_PatchedScreen":
        def record(name):
            def _fn(*args, **kw):
                self.calls.append((name, args, kw))
                if name == "add_button":
                    self.buttons.append(kw)
                if name == "add_combo":
                    self.combos.append(kw)
                if name == "add_checkbox":
                    self.checkboxes.append(kw)
                return kw.get("tag", name)

            return _fn

        def record_child_window(*args, **kw):
            self.calls.append(("child_window", args, kw))
            return _FakeContextManager()

        def record_table(*args, **kw):
            self.calls.append(("table", args, kw))
            self.tables.append(kw)
            return _FakeContextManager()

        def record_table_row(*args, **kw):
            self.calls.append(("table_row", args, kw))
            self.table_rows.append(kw)
            return _FakeContextManager()

        def record_table_column(*args, **kw):
            self.calls.append(("add_table_column", args, kw))
            self.columns.append(kw)
            return kw.get("tag", "add_table_column")

        self._cm = patch.multiple(
            "zd_app.ui.screens.device_vs_profile.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_combo=record("add_combo"),
            add_checkbox=record("add_checkbox"),
            child_window=record_child_window,
            group=_FakeGroup,
            table=record_table,
            table_row=record_table_row,
            add_table_column=record_table_column,
            does_item_exist=lambda *_a, **_kw: False,
            get_value=lambda *_a, **_kw: "",
        )
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False

    def build(self) -> None:
        screen.build(self.shell, parent="content_region")

    def text_strings(self) -> list[str]:
        out: list[str] = []
        for fn, args, kw in self.calls:
            if fn == "add_text":
                if args:
                    out.append(str(args[0]))
                elif "default_value" in kw:
                    out.append(str(kw["default_value"]))
        return out

    def status_chips(self) -> list[str]:
        return [s for s in self.text_strings() if s.startswith("■")]

    def button_tags(self) -> list[str]:
        return [kw.get("tag") for kw in self.buttons]

    def combo_tags(self) -> list[str]:
        return [kw.get("tag") for kw in self.combos]

    def column_labels(self) -> list[str]:
        return [kw.get("label") for kw in self.columns]


# ---------------------------------------------------------------------------
# Guard / empty branches
# ---------------------------------------------------------------------------


class GuardBranchTests(unittest.TestCase):
    def test_unavailable_when_store_is_none(self) -> None:
        sh = _shell(None)
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertIn("unavailable", joined)
        self.assertEqual(ps.combo_tags(), [])

    def test_no_profiles_renders_empty_state(self) -> None:
        sh = _shell(_FakeStore({}))
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertTrue("no wrapper profiles" in joined or "save a profile" in joined, joined)
        # No diff table without a profile to compare.
        self.assertEqual(ps.tables, [])

    def test_controls_render_with_profiles(self) -> None:
        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_1000)})
        sh = _shell(store, connected=False)
        with _PatchedScreen(sh) as ps:
            ps.build()
        self.assertIn(screen.TAG_PROFILE_COMBO, ps.combo_tags())
        self.assertIn(screen.TAG_READ_BUTTON, ps.button_tags())
        self.assertEqual(len(ps.checkboxes), 1)


# ---------------------------------------------------------------------------
# Disconnected device
# ---------------------------------------------------------------------------


class DisconnectedTests(unittest.TestCase):
    def test_no_device_banner_and_selected_only_table(self) -> None:
        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_1000, step_size=64)})
        sh = _shell(store, connected=False)
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertIn("not connected", joined)
        # The table still renders the profile's values.
        self.assertEqual(len(ps.tables), 1)

    def test_diff_table_wrapped_in_fill_height_scroll_region(self) -> None:
        # Scroll discipline: the comparison table is wrapped in a child_window
        # that fills the height remaining below the controls MINUS the caveat
        # footer reserve (height=-FOOTER_RESERVE_PX), so the table scrolls
        # INTERNALLY and the page (autosize_y root) does not grow a second bar
        # while the read-only caveat stays visible. A revert to the bare table
        # (whole-page scroll, caveat pushed off the bottom) fails here.
        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_1000, step_size=64)})
        sh = _shell(store, connected=False)
        with _PatchedScreen(sh) as ps:
            ps.build()
        scroll_windows = [
            kw
            for _fn, _a, kw in ps.calls
            if _fn == "child_window" and kw.get("tag") == screen.TAG_DIFF_SCROLL
        ]
        self.assertEqual(
            len(scroll_windows), 1,
            "expected exactly one diff-scroll child_window wrapping the table",
        )
        self.assertEqual(scroll_windows[0].get("height"), -screen.FOOTER_RESERVE_PX)
        # The caveat is still emitted (visible without a page scroll).
        self.assertIn(
            "read-only view",
            "\n".join(ps.text_strings()).lower(),
        )

    def test_disconnected_never_reads_the_device(self) -> None:
        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_1000)})
        rp = _FakeRPService(_snap(), {}, {})
        rp.read_current_state_with_provenance = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not read while disconnected")
        )
        sh = _shell(store, connected=False, rp_service=rp)
        with _PatchedScreen(sh) as ps:
            ps.build()
        rp.read_current_state_with_provenance.assert_not_called()


# ---------------------------------------------------------------------------
# Connected + live read
# ---------------------------------------------------------------------------


class ConnectedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeStore(
            {"race": _snap(polling_rate=PollingRate.HZ_500, step_size=64)}
        )
        # Current device: polling differs (changed), step_size matches (same).
        current = _snap(polling_rate=PollingRate.HZ_1000, step_size=64)
        success = {"polling_rate": True, "step_size": True}
        self.rp = _FakeRPService(current, success, {})

    def test_renders_diff_table_with_columns(self) -> None:
        sh = _shell(self.store, connected=True, rp_service=self.rp)
        with _PatchedScreen(sh) as ps:
            ps.build()
        from zd_app.i18n import t

        self.assertEqual(len(ps.tables), 1)
        self.assertEqual(
            ps.column_labels(),
            [
                t("device_vs_profile.col.field"),
                t("device_vs_profile.col.current"),
                t("device_vs_profile.col.selected"),
                t("device_vs_profile.col.status"),
            ],
        )

    def test_summary_reports_one_changed(self) -> None:
        sh = _shell(self.store, connected=True, rp_service=self.rp)
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("1 changed", joined)

    def test_changed_and_same_chips_present(self) -> None:
        state = screen.DeviceVsProfileScreenState(show_only_changes=False)
        sh = _shell(
            self.store,
            connected=True,
            rp_service=self.rp,
            screen_state=state,
        )
        with _PatchedScreen(sh) as ps:
            ps.build()
        chips = ps.status_chips()
        self.assertTrue(any("Changed" in c for c in chips), chips)
        self.assertTrue(any("Same" in c for c in chips), chips)

    def test_show_only_changes_defaults_to_true(self) -> None:
        state = screen.DeviceVsProfileScreenState()
        self.assertTrue(state.show_only_changes)

    def test_show_only_changes_filters_to_changed_rows(self) -> None:
        state = screen.DeviceVsProfileScreenState(show_only_changes=True)
        sh = _shell(self.store, connected=True, rp_service=self.rp, screen_state=state)
        with _PatchedScreen(sh) as ps:
            ps.build()
        chips = ps.status_chips()
        # Only the single changed row survives the filter.
        self.assertEqual(len(chips), 1)
        self.assertIn("Changed", chips[0])

    def test_read_failure_shows_banner_and_table(self) -> None:
        rp = _FakeRPService(_snap(), {}, {}, boom=True)
        sh = _shell(self.store, connected=True, rp_service=rp)
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertIn("could not read", joined)
        # The profile's values still render so the screen is not blank.
        self.assertEqual(len(ps.tables), 1)


# ---------------------------------------------------------------------------
# Mixed-encoding (encoding_differs) status rendering
# ---------------------------------------------------------------------------


_SENS_3 = (
    SensitivityAnchor(0, 0),
    SensitivityAnchor(50, 50),
    SensitivityAnchor(100, 100),
)
_SENS_8 = tuple(SensitivityAnchor(i * 10, i * 10) for i in range(8))


class EncodingDiffersChipTests(unittest.TestCase):
    def _build_mixed_encoding_screen(self):
        # Live 8-point-capable device vs a pre-8-point profile: the one-sided
        # rider folds to an encoding_differs row.
        store = _FakeStore({"apex final": _snap(sensitivity_left=_SENS_3)})
        rp = _FakeRPService(
            _snap(sensitivity_left=_SENS_3, sensitivity_left_8point=_SENS_8),
            {"sensitivity_left": True, "sensitivity_left_8point": True},
            {},
        )
        state = screen.DeviceVsProfileScreenState(show_only_changes=False)
        sh = _shell(store, connected=True, rp_service=rp, screen_state=state)
        with _PatchedScreen(sh) as ps:
            ps.build()
        return sh, ps

    def test_encoding_differs_chip_renders_translated_label(self) -> None:
        from zd_app.i18n import t

        label = t("device_vs_profile.status.encoding_differs")
        # The key must resolve (a missing key renders as "[key]").
        self.assertEqual(label, "Formats differ")
        _sh, ps = self._build_mixed_encoding_screen()
        chips = ps.status_chips()
        self.assertTrue(any(label in c for c in chips), chips)

    def test_encoding_differs_chip_uses_muted_bucket(self) -> None:
        from zd_app.i18n import t

        label = t("device_vs_profile.status.encoding_differs")
        sh, ps = self._build_mixed_encoding_screen()
        chip_calls = [
            kw
            for fn, args, kw in ps.calls
            if fn == "add_text" and args and str(args[0]) == f"■ {label}"
        ]
        self.assertTrue(chip_calls)
        for kw in chip_calls:
            self.assertEqual(kw.get("color"), sh.COLORS["muted"])


# ---------------------------------------------------------------------------
# Accent discipline: static section headers use text-primary, not accent
# ---------------------------------------------------------------------------


class SectionHeaderColorTests(unittest.TestCase):
    def test_section_headers_use_text_primary_not_accent(self) -> None:
        from zd_app.i18n import t

        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_1000)})
        rp = _FakeRPService(
            _snap(polling_rate=PollingRate.HZ_1000), {"polling_rate": True}, {}
        )
        state = screen.DeviceVsProfileScreenState(show_only_changes=False)
        sh = _shell(store, connected=True, rp_service=rp, screen_state=state)
        with _PatchedScreen(sh) as ps:
            ps.build()
        header_labels = {
            t(f"device_vs_profile.section.{cat}") for cat in screen._SECTION_ORDER
        }
        header_calls = [
            kw
            for fn, args, kw in ps.calls
            if fn == "add_text" and args and str(args[0]) in header_labels
        ]
        self.assertTrue(header_calls)
        for kw in header_calls:
            self.assertEqual(kw.get("color"), sh.COLORS["text"])
            self.assertNotEqual(kw.get("color"), sh.COLORS["accent"])


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class CallbackTests(unittest.TestCase):
    def test_profile_change_updates_state_and_rebuilds(self) -> None:
        state = screen.DeviceVsProfileScreenState(selected_profile_id="race")
        sh = _shell(_FakeStore({"race": _snap(), "drift": _snap()}), screen_state=state)
        screen._on_profile_changed(sh, "drift")
        self.assertEqual(state.selected_profile_id, "drift")
        sh.rebuild_current_screen.assert_called()

    def test_read_button_resets_read_flag_and_rebuilds(self) -> None:
        state = screen.DeviceVsProfileScreenState(read_attempted=True)
        sh = _shell(_FakeStore({"race": _snap()}), screen_state=state)
        screen._on_read_clicked(sh)
        self.assertFalse(state.read_attempted)
        sh.rebuild_current_screen.assert_called()

    def test_show_only_changes_toggle_updates_state(self) -> None:
        state = screen.DeviceVsProfileScreenState(show_only_changes=True)
        sh = _shell(_FakeStore({"race": _snap()}), screen_state=state)
        screen._on_show_only_changes(sh, False)
        self.assertFalse(state.show_only_changes)
        sh.rebuild_current_screen.assert_called()


# ---------------------------------------------------------------------------
# i18n parity for the new device_vs_profile.* keys
# ---------------------------------------------------------------------------


class I18nParityTests(unittest.TestCase):
    def setUp(self) -> None:
        import json
        from zd_app.i18n import _locale_dir

        locale_dir = _locale_dir()
        with open(locale_dir / "en.json", encoding="utf-8") as f:
            self.en = json.load(f)
        with open(locale_dir / "zh-CN.json", encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_device_vs_profile_keys_have_locale_parity(self) -> None:
        en_keys = {k for k in self.en if k.startswith("device_vs_profile.")}
        zh_keys = {k for k in self.zh if k.startswith("device_vs_profile.")}
        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 25)

    def test_nav_key_present_in_both(self) -> None:
        self.assertIn("nav.device_vs_profile", self.en)
        self.assertIn("nav.device_vs_profile", self.zh)

    def test_no_empty_values(self) -> None:
        for key, value in self.en.items():
            if key.startswith("device_vs_profile.") or key == "nav.device_vs_profile":
                self.assertTrue(str(value).strip(), f"empty en value for {key}")
        for key, value in self.zh.items():
            if key.startswith("device_vs_profile.") or key == "nav.device_vs_profile":
                self.assertTrue(str(value).strip(), f"empty zh-CN value for {key}")


class RealContextRenderTests(unittest.TestCase):
    """Build against a REAL DPG context to catch a malformed table.

    The recorder stubs ``table`` / ``table_row`` and never touches Dear PyGui,
    so it cannot catch a bad column policy / unsupported flag / autosize clash.
    This drives the actual widgets (like ``test_wear_ledger_screen``) and
    inspects the live item tree.
    """

    def _build_in_context(self, shell) -> None:
        import dearpygui.dearpygui as dpg

        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        screen.build(shell, parent="content_region")

    def test_connected_build_renders_real_table(self) -> None:
        import dearpygui.dearpygui as dpg

        store = _FakeStore({"race": _snap(polling_rate=PollingRate.HZ_500, step_size=64)})
        rp = _FakeRPService(
            _snap(polling_rate=PollingRate.HZ_1000, step_size=64),
            {"polling_rate": True, "step_size": True},
            {},
        )
        sh = _shell(store, connected=True, rp_service=rp)
        try:
            self._build_in_context(sh)
            self.assertTrue(dpg.does_item_exist(screen.TAG_DIFF_TABLE))
            self.assertEqual(
                dpg.get_item_type(screen.TAG_DIFF_TABLE), "mvAppItemType::mvTable"
            )
        finally:
            dpg.destroy_context()

    def test_no_profiles_build_renders_no_table(self) -> None:
        import dearpygui.dearpygui as dpg

        sh = _shell(_FakeStore({}))
        try:
            self._build_in_context(sh)
            self.assertFalse(dpg.does_item_exist(screen.TAG_DIFF_TABLE))
        finally:
            dpg.destroy_context()


# ---------------------------------------------------------------------------
# Phase 2 — the Last-Applied column (record header, third value column, drift)
# ---------------------------------------------------------------------------
# Appended as a self-contained block (imports included) so the Phase-1 suite
# above stays byte-identical.

from datetime import datetime, timedelta, timezone  # noqa: E402 - Phase-2 block

from zd_app.storage.last_applied_store import LastAppliedRecord  # noqa: E402


def _record_ts(hours_ago: int = 2) -> str:
    # Now-relative (time-rot rule) — never a calendar date.
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeLastAppliedStore:
    def __init__(self, record: LastAppliedRecord | None) -> None:
        self._record = record

    def load(self) -> LastAppliedRecord | None:
        return self._record


def _attach_record(sh, snapshot, *, failed=(), name="race day") -> LastAppliedRecord:
    record = LastAppliedRecord(
        profile_name=name,
        applied_at=_record_ts(),
        include_device=True,
        failed_fields=tuple(failed),
        snapshot=snapshot,
    )
    sh.last_applied_store = _FakeLastAppliedStore(record)
    return record


class LastAppliedColumnTests(unittest.TestCase):
    """A record exists → header line + the third value column + drift cells."""

    def _connected_shell(self):
        store = _FakeStore(
            {"race day": _snap(polling_rate=PollingRate.HZ_500, step_size=64)}
        )
        current = _snap(polling_rate=PollingRate.HZ_1000, step_size=64)
        rp = _FakeRPService(
            current, {"polling_rate": True, "step_size": True}, {}
        )
        return _shell(store, connected=True, rp_service=rp)

    def test_third_column_and_header_render_with_record(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()
        record = _attach_record(
            sh, _snap(polling_rate=PollingRate.HZ_8000, step_size=64)
        )
        with _PatchedScreen(sh) as ps:
            ps.build()
        self.assertEqual(
            ps.column_labels(),
            [
                t("device_vs_profile.col.field"),
                t("device_vs_profile.col.current"),
                t("device_vs_profile.col.selected"),
                t("device_vs_profile.col.last_applied"),
                t("device_vs_profile.col.status"),
            ],
        )
        header = t(
            "device_vs_profile.last_applied_header",
            name=record.profile_name,
            ts=record.applied_at,
        )
        self.assertIn(header, ps.text_strings())

    def test_summary_gains_drift_count_with_record(self) -> None:
        sh = self._connected_shell()
        # polling drifted (device 1000 vs applied 8000); step_size matches.
        _attach_record(sh, _snap(polling_rate=PollingRate.HZ_8000, step_size=64))
        with _PatchedScreen(sh) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("1 drifted", joined)

    def test_no_record_keeps_phase1_layout_and_shows_hint(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()  # no last_applied_store attribute at all
        with _PatchedScreen(sh) as ps:
            ps.build()
        self.assertEqual(
            ps.column_labels(),
            [
                t("device_vs_profile.col.field"),
                t("device_vs_profile.col.current"),
                t("device_vs_profile.col.selected"),
                t("device_vs_profile.col.status"),
            ],
        )
        self.assertIn(t("device_vs_profile.no_apply_recorded"), ps.text_strings())

    def test_empty_store_shows_hint_not_column(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()
        sh.last_applied_store = _FakeLastAppliedStore(None)  # store wired, no record
        with _PatchedScreen(sh) as ps:
            ps.build()
        self.assertEqual(len(ps.columns), 4)
        self.assertIn(t("device_vs_profile.no_apply_recorded"), ps.text_strings())

    def test_drifted_cell_uses_warn_color_and_text_marker(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()
        _attach_record(sh, _snap(polling_rate=PollingRate.HZ_8000, step_size=64))
        with _PatchedScreen(sh) as ps:
            ps.build()
        marker = f"({t('device_vs_profile.status.drifted')})"
        drift_cells = [
            (args, kw)
            for fn, args, kw in ps.calls
            if fn == "add_text" and args and marker in str(args[0])
        ]
        self.assertEqual(len(drift_cells), 1, drift_cells)
        _args, kw = drift_cells[0]
        self.assertEqual(kw.get("color"), sh.COLORS["warn"])

    def test_matching_cell_is_muted_without_marker(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()
        _attach_record(sh, _snap(polling_rate=PollingRate.HZ_1000, step_size=64))
        with _PatchedScreen(sh) as ps:
            ps.build()
        marker = f"({t('device_vs_profile.status.drifted')})"
        self.assertFalse(any(marker in s for s in ps.text_strings()))
        joined = "\n".join(ps.text_strings())
        self.assertIn("0 drifted", joined)

    def test_failed_at_apply_cell_renders_caveat_muted(self) -> None:
        from zd_app.i18n import t

        sh = self._connected_shell()
        _attach_record(
            sh,
            _snap(polling_rate=PollingRate.HZ_8000, step_size=64),
            failed=("polling",),
        )
        with _PatchedScreen(sh) as ps:
            ps.build()
        marker = f"({t('device_vs_profile.status.apply_failed_field')})"
        failed_cells = [
            (args, kw)
            for fn, args, kw in ps.calls
            if fn == "add_text" and args and marker in str(args[0])
        ]
        self.assertEqual(len(failed_cells), 1, failed_cells)
        _args, kw = failed_cells[0]
        self.assertEqual(kw.get("color"), sh.COLORS["muted"])
        # A failed field is never counted as drift.
        self.assertIn("0 drifted", "\n".join(ps.text_strings()))

    def test_disconnected_branch_still_shows_record_column(self) -> None:
        from zd_app.i18n import t

        store = _FakeStore({"race day": _snap(polling_rate=PollingRate.HZ_500)})
        sh = _shell(store, connected=False)
        record = _attach_record(sh, _snap(polling_rate=PollingRate.HZ_8000))
        with _PatchedScreen(sh) as ps:
            ps.build()
        self.assertEqual(len(ps.columns), 5)
        header = t(
            "device_vs_profile.last_applied_header",
            name=record.profile_name,
            ts=record.applied_at,
        )
        self.assertIn(header, ps.text_strings())
        # No device → no drift verdicts anywhere (drift needs a readable
        # current side), so the warn-marker never appears.
        marker = f"({t('device_vs_profile.status.drifted')})"
        self.assertFalse(any(marker in s for s in ps.text_strings()))


class LastAppliedI18nKeyTests(unittest.TestCase):
    """The six Phase-2 keys exist with non-empty values in BOTH locales.

    (The prefix-wide parity sweep in I18nParityTests covers set-equality;
    this pins the exact keys so a rename can't silently drop one.)
    """

    _NEW_KEYS = (
        "device_vs_profile.col.last_applied",
        "device_vs_profile.last_applied_header",
        "device_vs_profile.no_apply_recorded",
        "device_vs_profile.status.drifted",
        "device_vs_profile.status.apply_failed_field",
        "device_vs_profile.summary_with_drift",
    )

    def test_new_keys_present_and_non_empty_in_both_locales(self) -> None:
        import json
        from zd_app.i18n import _locale_dir

        locale_dir = _locale_dir()
        with open(locale_dir / "en.json", encoding="utf-8") as f:
            en = json.load(f)
        with open(locale_dir / "zh-CN.json", encoding="utf-8") as f:
            zh = json.load(f)
        for key in self._NEW_KEYS:
            self.assertIn(key, en)
            self.assertIn(key, zh)
            self.assertTrue(str(en[key]).strip(), f"empty en value for {key}")
            self.assertTrue(str(zh[key]).strip(), f"empty zh-CN value for {key}")


class LastAppliedRealContextRenderTests(unittest.TestCase):
    """Five-column build against a REAL DPG context (mirrors RealContextRenderTests)."""

    def test_record_build_renders_real_five_column_table(self) -> None:
        import dearpygui.dearpygui as dpg

        store = _FakeStore(
            {"race day": _snap(polling_rate=PollingRate.HZ_500, step_size=64)}
        )
        rp = _FakeRPService(
            _snap(polling_rate=PollingRate.HZ_1000, step_size=64),
            {"polling_rate": True, "step_size": True},
            {},
        )
        sh = _shell(store, connected=True, rp_service=rp)
        _attach_record(sh, _snap(polling_rate=PollingRate.HZ_8000, step_size=64))
        dpg.create_context()
        try:
            with dpg.window():
                with dpg.child_window(tag="content_region"):
                    pass
            screen.build(sh, parent="content_region")
            self.assertTrue(dpg.does_item_exist(screen.TAG_DIFF_TABLE))
            self.assertEqual(
                dpg.get_item_type(screen.TAG_DIFF_TABLE), "mvAppItemType::mvTable"
            )
        finally:
            dpg.destroy_context()


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
