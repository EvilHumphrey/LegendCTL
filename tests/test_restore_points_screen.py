"""DPG screen-build tests for the Restore Points screen.

Patches ``dearpygui.dearpygui`` symbols on the screen module so ``build``
runs without a real DPG context. Each test verifies the right container
tags appear for the corresponding view (LIST / DETAIL / CONFIRM /
IN_PROGRESS / RESULT), and that callbacks wired to buttons transition the
screen state and invoke the service correctly.

The :class:`RestorePointService` is stubbed via :class:`_FakeService` so
the tests are fully deterministic and never touch the filesystem.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

from zd_app.services.restore_points import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)
from zd_app.services.settings_service import (
    AxisInversion,
    ButtonMapping,
    ButtonSlot,
    ControllerButtonTarget,
    ControllerSnapshot,
    LightingMode,
    LightingSettings,
    LightingZone,
    PollingRate,
    RgbColor,
    SensitivityAnchor,
    StickDeadzones,
    TriggerMode,
    TriggerSettings,
    TriggerVibrationMode,
    VibrationSettings,
)
from zd_app.storage.restore_point_models import (
    KIND,
    SCHEMA_VERSION,
    CaptureSource,
    CoverageCategory,
    CoverageState,
    DeviceIdentity,
    FieldCoverage,
    IdentityConfidence,
    RestoreFieldDelta,
    RestoreFieldOutcome,
    RestorePoint,
    RestorePointCoverage,
    RestorePointTrigger,
    RestorePreview,
    RestoreResult,
    RestoreResultLabel,
    SkippedFile,
)
from zd_app.ui.screens import restore_points as screen


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


def _snapshot() -> ControllerSnapshot:
    return ControllerSnapshot(
        polling_rate=PollingRate.HZ_1000,
        vibration=VibrationSettings(10, 20, 30, 40, TriggerVibrationMode.NATIVE),
        deadzones=StickDeadzones(5, 6, 90, 91),
        axis_inversion_left=AxisInversion(False, False),
        axis_inversion_right=AxisInversion(False, False),
        sensitivity_left=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        sensitivity_right=(
            SensitivityAnchor(0, 0),
            SensitivityAnchor(50, 50),
            SensitivityAnchor(100, 100),
        ),
        trigger_left=TriggerSettings(0, 100, TriggerMode.LONG),
        trigger_right=TriggerSettings(0, 100, TriggerMode.LONG),
        button_bindings={
            ButtonSlot.A: ButtonMapping.controller_button(ControllerButtonTarget.B),
        },
        lighting_zones={
            LightingZone.HOME: LightingSettings(
                True, LightingMode.ALWAYS_ON, 200, RgbColor(10, 20, 30)
            ),
        },
        motion_settings=None,
        back_paddle_bindings={},
        step_size=128,
    )


def _coverage() -> RestorePointCoverage:
    return RestorePointCoverage(
        captured_supported_count=8,
        total_supported_count=13,
        capture_source=CaptureSource.FRESH_READ,
        fields={
            "polling_rate": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=True,
                category=CoverageCategory.DEVICE,
            ),
            "step_size": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=True,
                category=CoverageCategory.DEVICE,
            ),
            "deadzones": FieldCoverage(
                state=CoverageState.CAPTURED,
                readable=True,
                writable=True,
                category=CoverageCategory.FEEL,
            ),
            # COSMETIC because rumble strength is presentation —
            # mirrors restore_point_service._SCALAR_FIELDS.
            "vibration": FieldCoverage(
                state=CoverageState.NOT_CAPTURED,
                readable=False,
                writable=True,
                category=CoverageCategory.COSMETIC,
            ),
            "lighting_zones": FieldCoverage(
                state=CoverageState.PARTIAL,
                readable=True,
                writable=True,
                category=CoverageCategory.COSMETIC,
                note="Captured 1 of 3 supported entries.",
            ),
            "motion_settings": FieldCoverage(
                state=CoverageState.UNSUPPORTED,
                readable=False,
                writable=False,
                category=CoverageCategory.UNSUPPORTED,
                note="No supported write path in this app.",
            ),
        },
    )


def _rp(id: str = "rp_20260524_190355_9b42de", title: Optional[str] = None) -> RestorePoint:
    return RestorePoint(
        schema_version=SCHEMA_VERSION,
        kind=KIND,
        id=id,
        created_at="2026-05-24T19:03:55Z",
        app_version="2.0.0",
        app_build_commit=None,
        title=title or "Before Safe Import — 2026-05-24 19:03",
        trigger=RestorePointTrigger(
            type="before_safe_import_apply",
            source_label="Safe Import",
            reason="Created before applying imported profile to controller",
        ),
        device_identity=DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string="ZD Ultimate Legend",
            firmware_version="1.18",
            identity_confidence=IdentityConfidence.READABLE,
        ),
        snapshot=_snapshot(),
        coverage=_coverage(),
        last_restore_attempt=None,
    )


def _result() -> RestoreResult:
    return RestoreResult(
        label=RestoreResultLabel.VERIFIED,
        attempted=3,
        wrote_succeeded=3,
        write_failed=0,
        verified_matched=3,
        could_not_verify=0,
        mismatched=0,
        fields=(
            RestoreFieldOutcome(
                field_name="polling_rate",
                write_succeeded=True,
                write_error=None,
                verify_matched=True,
            ),
        ),
        before_restore_point_id="rp_before_test",
        completed_at="2026-05-24T19:05:30Z",
    )


class _FakeService:
    """Stand-in for RestorePointService recording capture + restore calls."""

    def __init__(
        self,
        *,
        valid: Optional[list[RestorePoint]] = None,
        skipped: Optional[list[SkippedFile]] = None,
        restore_result: Optional[RestoreResult] = None,
        restore_raises: Optional[Exception] = None,
        preview: Optional[RestorePreview] = None,
        preview_raises: Optional[Exception] = None,
        delete_returns: bool = True,
    ) -> None:
        self._valid = valid or []
        self._skipped = skipped or []
        self._restore_result = restore_result or _result()
        self._restore_raises = restore_raises
        self._preview = preview
        self._preview_raises = preview_raises
        self._delete_returns = delete_returns
        # Private-but-tested: the screen uses ``service._store.load(rp_id)``
        # for the detail view to avoid pulling the whole list on every nav.
        self._store = MagicMock()
        self._store.load.side_effect = self._fake_load
        self.calls: list[tuple[str, tuple, dict]] = []

    def _fake_load(self, rp_id: str) -> RestorePoint:
        for rp in self._valid:
            if rp.id == rp_id:
                return rp
        raise FileNotFoundError(rp_id)

    def list_with_skipped(self):
        self.calls.append(("list_with_skipped", (), {}))
        return list(self._valid), list(self._skipped)

    def restore(self, rp_id: str, *, excluded_fields=None):
        self.calls.append(("restore", (rp_id,), {"excluded_fields": excluded_fields}))
        if self._restore_raises is not None:
            raise self._restore_raises
        return self._restore_result

    def delete(self, rp_id: str) -> bool:
        self.calls.append(("delete", (rp_id,), {}))
        return self._delete_returns

    def compute_restore_preview(self, rp_id: str) -> RestorePreview:
        self.calls.append(("compute_restore_preview", (rp_id,), {}))
        if self._preview_raises is not None:
            raise self._preview_raises
        if self._preview is None:
            # Default: no-op preview so existing tests that didn't pass one
            # in still render an empty modal without surprises. New tests
            # injecting a preview get exactly the shape they configured.
            return RestorePreview(
                fields=(),
                fields_changing=0,
                fields_unchanged=0,
                fields_unreadable=0,
            )
        return self._preview


def _shell_with(
    service: Optional[_FakeService],
    *,
    screen_state: Optional[screen.RestorePointsScreenState] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        restore_point_service=service,
        restore_points_screen_state=screen_state,
        COLORS={
            "accent": (1, 1, 1, 1),
            "muted": (2, 2, 2, 2),
            "warn": (3, 3, 3, 3),
            "good": (4, 4, 4, 4),
            "text": (5, 5, 5, 5),
        },
        rebuild_current_screen=MagicMock(),
    )


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _FakeChildWindow(*_args, **_kw):
    return _FakeContextManager()


def _FakeGroup(*_args, **_kw):
    return _FakeContextManager()


class _PatchedScreen:
    """Context manager that captures DPG calls + button callbacks."""

    def __init__(self, shell) -> None:
        self.shell = shell
        self.values: dict[str, object] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.buttons: list[dict] = []
        self.child_windows: list[dict] = []
        self.tables: list[dict] = []
        self.table_columns: list[dict] = []
        self.windows: list[dict] = []
        self.deleted_items: list[object] = []
        self._cm = None

    def __enter__(self) -> "_PatchedScreen":
        def record(name):
            def _fn(*args, **kw):
                self.calls.append((name, args, kw))
                if name == "add_button":
                    self.buttons.append(kw)
                elif name == "add_table_column":
                    self.table_columns.append(kw)
                return kw.get("tag", name)
            return _fn

        def record_child_window(*args, **kw):
            self.calls.append(("child_window", args, kw))
            self.child_windows.append(kw)
            return _FakeContextManager()

        def record_table(*args, **kw):
            self.calls.append(("table", args, kw))
            self.tables.append(kw)
            return _FakeContextManager()

        def record_table_row(*args, **kw):
            self.calls.append(("table_row", args, kw))
            return _FakeContextManager()

        def record_window(*args, **kw):
            self.calls.append(("window", args, kw))
            self.windows.append(kw)
            return _FakeContextManager()

        def record_delete_item(item, **kw):
            self.calls.append(("delete_item", (item,), kw))
            self.deleted_items.append(item)

        def set_value(tag, value):
            self.values[tag] = value

        # The list view renders a dpg.table inside a card(). The
        # screen module, components.py, and typography.py all do
        # ``import dearpygui.dearpygui as dpg`` — the SAME module object — so
        # patching its attributes here routes card()/section_title()/the table
        # family through this recorder too (no separate components/typography
        # patch needed). bind_item_theme is stubbed because card()'s theme bind
        # may fire if a prior test left components._CARD_THEME set.
        self._cm = patch.multiple(
            "zd_app.ui.screens.restore_points.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_progress_bar=record("add_progress_bar"),
            add_table_column=record("add_table_column"),
            child_window=record_child_window,
            table=record_table,
            table_row=record_table_row,
            group=_FakeGroup,
            window=record_window,
            delete_item=record_delete_item,
            set_value=set_value,
            bind_item_theme=lambda *_a, **_kw: None,
            does_item_exist=lambda *_a, **_kw: True,
            get_frame_count=lambda: 0,
            set_frame_callback=record("set_frame_callback"),
        )
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False

    def build(self) -> None:
        screen.build(self.shell, parent="content_region")

    def button_tags(self) -> list[str]:
        return [kw.get("tag") for kw in self.buttons]

    def text_strings(self) -> list[str]:
        result: list[str] = []
        for fn, args, kw in self.calls:
            if fn == "add_text":
                if args:
                    result.append(str(args[0]))
                elif "default_value" in kw:
                    result.append(str(kw["default_value"]))
        return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class UnavailableServiceTests(unittest.TestCase):
    def test_renders_unavailable_text_when_service_is_none(self) -> None:
        shell = _shell_with(None)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("unavailable", all_text.lower())


class ListViewTests(unittest.TestCase):
    def test_empty_state_renders_empty_copy(self) -> None:
        service = _FakeService(valid=[], skipped=[])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("No restore points yet", all_text)
        # The empty state must NEVER claim factory backups (per the forbidden-phrase policy
        # — the deny "they are not factory backups" copy is allowed
        # precisely because it denies, but the test just checks the
        # well-formed empty-state copy is the one we render).
        self.assertIn("not factory backups", all_text)

    def test_skipped_footer_renders_when_only_skipped_files(self) -> None:
        skipped = [
            SkippedFile(path="C:/temp/rp_bad_1.json", error="corrupt"),
            SkippedFile(path="C:/temp/rp_bad_2.json", error="schema"),
        ]
        service = _FakeService(valid=[], skipped=skipped)
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        button_tags = ps.button_tags()
        all_text = " ".join(ps.text_strings())
        self.assertIn(screen.TAG_LIST_SKIPPED_TOGGLE, button_tags)
        self.assertIn("2 restore point(s) could not be read", all_text)

    def test_list_row_renders_rp_metadata(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn(rp.title, all_text)
        self.assertIn(rp.created_at, all_text)
        self.assertIn("ZD Ultimate Legend", all_text)
        self.assertIn("Captured 8 of 13", all_text)
        self.assertIn("Never restored", all_text)

    def test_refresh_button_rebuilds_screen(self) -> None:
        service = _FakeService(valid=[])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_LIST_REFRESH_BUTTON:
                    kw["callback"]()
                    break
        shell.rebuild_current_screen.assert_called_once()

    def test_list_footer_contains_short_claim_boundary(self) -> None:
        service = _FakeService(valid=[_rp()])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn(CLAIM_BOUNDARY_SHORT_UI, all_text)


class ManualSaveButtonTests(unittest.TestCase):
    """The manual "Save Restore Point" capture button.

    Relocated here from Diagnostics (operator decision 2026-06-21): this is
    the app's ONLY manual-capture entry point, and the list empty-state copy
    already tells users they "can save one manually after reading the
    controller." It delegates to ``AppShell.manual_save_restore_point`` (which
    returns the new RP, or ``None`` when it fails / is refused while a HID job
    is in flight) and surfaces the outcome in the screen's status line.
    """

    def setUp(self) -> None:
        from zd_app import i18n

        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _shell(self, service, *, capture_return):
        shell = _shell_with(service)
        shell.manual_save_restore_point = MagicMock(return_value=capture_return)
        return shell

    def test_manual_save_button_renders_in_list_view(self) -> None:
        service = _FakeService(valid=[_rp()])
        shell = self._shell(service, capture_return=_rp())
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertIn(screen.TAG_LIST_MANUAL_SAVE_BUTTON, ps.button_tags())

    def test_manual_save_success_captures_rebuilds_and_reports(self) -> None:
        from zd_app import i18n

        service = _FakeService(valid=[])
        shell = self._shell(service, capture_return=_rp(id="rp_manual_new"))
        with _PatchedScreen(shell) as ps:
            ps.build()
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_LIST_MANUAL_SAVE_BUTTON:
                    kw["callback"]()
                    break
        shell.manual_save_restore_point.assert_called_once()
        shell.rebuild_current_screen.assert_called_once()
        state = shell.restore_points_screen_state
        self.assertEqual(state.status_kind, "info")
        self.assertEqual(state.status_text, i18n.t("restore_points.manual.success"))

    def test_manual_save_failure_sets_warn_status(self) -> None:
        # ``manual_save_restore_point`` returns None on failure or HID-busy
        # refusal; the screen surfaces a warn-coloured status line.
        service = _FakeService(valid=[])
        shell = self._shell(service, capture_return=None)
        with _PatchedScreen(shell) as ps:
            ps.build()
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_LIST_MANUAL_SAVE_BUTTON:
                    kw["callback"]()
                    break
        shell.manual_save_restore_point.assert_called_once()
        shell.rebuild_current_screen.assert_called_once()
        state = shell.restore_points_screen_state
        self.assertEqual(state.status_kind, "warn")
        self.assertIn("Could not save", state.status_text)


class DetailViewTests(unittest.TestCase):
    def _open_detail(self, service: _FakeService, rp_id: str) -> _PatchedScreen:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_DETAIL, selected_rp_id=rp_id
        )
        shell = _shell_with(service, screen_state=state)
        ps = _PatchedScreen(shell).__enter__()
        ps.build()
        return ps

    def test_detail_view_renders_verbatim_claim_boundary(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_detail(service, rp.id)
        try:
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(CLAIM_BOUNDARY_PARAGRAPH, all_text)

    def test_detail_view_renders_restore_button(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_detail(service, rp.id)
        try:
            tags = ps.button_tags()
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(screen.TAG_DETAIL_RESTORE_BUTTON, tags)
        self.assertIn(screen.TAG_DETAIL_EXPORT_BUTTON, tags)
        self.assertIn(screen.TAG_DETAIL_BACK_BUTTON, tags)


class ConfirmViewTests(unittest.TestCase):
    def _open_confirm(self, service: _FakeService, rp_id: str) -> _PatchedScreen:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_CONFIRM, selected_rp_id=rp_id
        )
        shell = _shell_with(service, screen_state=state)
        ps = _PatchedScreen(shell).__enter__()
        ps.build()
        return ps

    def test_confirm_view_renders_3_way_buttons(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_confirm(service, rp.id)
        try:
            tags = ps.button_tags()
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(screen.TAG_CONFIRM_RESTORE_BUTTON, tags)
        self.assertIn(screen.TAG_CONFIRM_VIEW_BUTTON, tags)
        self.assertIn(screen.TAG_CONFIRM_CANCEL_BUTTON, tags)

    def test_confirm_restore_button_transitions_to_in_progress(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_confirm(service, rp.id)
        try:
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_CONFIRM_RESTORE_BUTTON:
                    kw["callback"]()
                    break
        finally:
            ps.__exit__(None, None, None)
        state = ps.shell.restore_points_screen_state
        self.assertEqual(state.view, screen.VIEW_IN_PROGRESS)
        ps.shell.rebuild_current_screen.assert_called()

    def test_confirm_cancel_returns_to_detail(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_confirm(service, rp.id)
        try:
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_CONFIRM_CANCEL_BUTTON:
                    kw["callback"]()
                    break
        finally:
            ps.__exit__(None, None, None)
        state = ps.shell.restore_points_screen_state
        self.assertEqual(state.view, screen.VIEW_DETAIL)

    def test_confirm_preview_renders_heading_and_changing_fields(self) -> None:
        """Pre-restore preview: the CONFIRM modal now renders a
        "What this restore will change:" section showing every field
        where the captured snapshot differs from the live device. Fixes
        the asymmetry with the restore-result-enrichment result-page render —
        BEFORE the restore, the user can now see what's about to change.
        """
        rp = _rp()
        preview = RestorePreview(
            fields=(
                RestoreFieldDelta(
                    field_name="step_size",
                    will_change=True,
                    current_value="75",
                    target_value="128",
                ),
                RestoreFieldDelta(
                    field_name="polling_rate",
                    will_change=True,
                    current_value="PollingRate.HZ_8000",
                    target_value="PollingRate.HZ_1000",
                ),
            ),
            fields_changing=2,
            fields_unchanged=0,
            fields_unreadable=0,
        )
        service = _FakeService(valid=[rp], preview=preview)
        ps = self._open_confirm(service, rp.id)
        try:
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn("What this restore will change", all_text)
        self.assertIn("step_size: 75 → 128", all_text)
        self.assertIn("polling_rate: PollingRate.HZ_8000 → PollingRate.HZ_1000", all_text)

    def test_confirm_preview_omits_unchanged_fields(self) -> None:
        """The preview lists only changing fields — unchanged ones are
        suppressed to keep the modal short. The user can still trust the
        absence of a row to mean "unchanged" (vs. the result page, which
        shows every attempted field including the matches).
        """
        rp = _rp()
        preview = RestorePreview(
            fields=(
                RestoreFieldDelta(
                    field_name="step_size",
                    will_change=True,
                    current_value="75",
                    target_value="128",
                ),
                RestoreFieldDelta(
                    field_name="polling_rate",
                    will_change=False,
                    current_value="PollingRate.HZ_1000",
                    target_value="PollingRate.HZ_1000",
                ),
            ),
            fields_changing=1,
            fields_unchanged=1,
            fields_unreadable=0,
        )
        service = _FakeService(valid=[rp], preview=preview)
        ps = self._open_confirm(service, rp.id)
        try:
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn("step_size: 75 → 128", all_text)
        # polling_rate is unchanged — must NOT render as a delta line.
        # (It may incidentally appear elsewhere in the modal copy, so the
        # specific check is that the "→" arrow isn't beside it.)
        self.assertNotIn("polling_rate: ", all_text)

    def test_confirm_preview_renders_unreadable_note_when_present(self) -> None:
        """When ``fields_unreadable > 0``, the modal appends a small note
        explaining the device couldn't be read for that many fields —
        they'll still be restored, but the user won't see the diff.
        """
        rp = _rp()
        preview = RestorePreview(
            fields=(
                RestoreFieldDelta(
                    field_name="polling_rate",
                    will_change=False,
                    current_value=None,
                    target_value="PollingRate.HZ_1000",
                    note=(
                        "device read failed: TimeoutError: "
                        "HID read timed out after 967ms"
                    ),
                ),
                RestoreFieldDelta(
                    field_name="step_size",
                    will_change=True,
                    current_value="75",
                    target_value="128",
                ),
            ),
            fields_changing=1,
            fields_unchanged=0,
            fields_unreadable=1,
        )
        service = _FakeService(valid=[rp], preview=preview)
        ps = self._open_confirm(service, rp.id)
        try:
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn("1 fields couldn't be read", all_text)
        # The unreadable field itself is NOT rendered as a delta line —
        # it has will_change=False and no current_value to compare against.
        self.assertNotIn("polling_rate: ", all_text)

    def test_confirm_preview_renders_no_op_when_nothing_differs(self) -> None:
        """When the live device matches the captured RP exactly, the
        modal renders the no-op copy so the user knows the restore won't
        actually change anything (can still proceed or cancel).
        """
        rp = _rp()
        preview = RestorePreview(
            fields=(
                RestoreFieldDelta(
                    field_name="polling_rate",
                    will_change=False,
                    current_value="PollingRate.HZ_1000",
                    target_value="PollingRate.HZ_1000",
                ),
            ),
            fields_changing=0,
            fields_unchanged=1,
            fields_unreadable=0,
        )
        service = _FakeService(valid=[rp], preview=preview)
        ps = self._open_confirm(service, rp.id)
        try:
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn("No changes detected", all_text)
        # No-op state must still render the heading so the user knows
        # the preview ran (vs. believing the preview was just missing).
        self.assertIn("What this restore will change", all_text)

    def test_confirm_preview_swallows_unexpected_service_failure(self) -> None:
        """Defensive: if ``compute_restore_preview`` raises (e.g. store
        load failure mid-build), the CONFIRM modal still renders the
        action buttons so the user can Cancel out. The preview section
        is simply omitted.
        """
        rp = _rp()
        service = _FakeService(
            valid=[rp], preview_raises=RuntimeError("HID brain freeze")
        )
        ps = self._open_confirm(service, rp.id)
        try:
            tags = ps.button_tags()
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(screen.TAG_CONFIRM_RESTORE_BUTTON, tags)
        self.assertIn(screen.TAG_CONFIRM_CANCEL_BUTTON, tags)
        self.assertNotIn("What this restore will change", all_text)


class InProgressViewTests(unittest.TestCase):
    def test_in_progress_view_renders_spinner_text(self) -> None:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_IN_PROGRESS, selected_rp_id="rp_test"
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("Restoring", all_text)

    def test_execute_restore_transitions_to_result_on_success(self) -> None:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_IN_PROGRESS, selected_rp_id="rp_test"
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        screen._execute_restore(shell)
        self.assertEqual(shell.restore_points_screen_state.view, screen.VIEW_RESULT)
        self.assertIsNotNone(shell.restore_points_screen_state.result)
        self.assertEqual(
            shell.restore_points_screen_state.result.label,
            RestoreResultLabel.VERIFIED,
        )

    def test_execute_restore_transitions_to_list_on_exception(self) -> None:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_IN_PROGRESS, selected_rp_id="rp_test"
        )
        service = _FakeService(
            valid=[_rp(id="rp_test")],
            restore_raises=RuntimeError("HID timeout"),
        )
        shell = _shell_with(service, screen_state=state)
        screen._execute_restore(shell)
        self.assertEqual(shell.restore_points_screen_state.view, screen.VIEW_LIST)
        self.assertIn("HID timeout", shell.restore_points_screen_state.status_text)


class ResultViewTests(unittest.TestCase):
    def test_result_view_renders_counts_first(self) -> None:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=_result(),
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        # Per the "Post-restore verification" rule — counts first.
        self.assertIn("Attempted: 3", all_text)
        self.assertIn("Wrote successfully: 3", all_text)
        self.assertIn("Verified matched: 3", all_text)
        self.assertIn("Verified", all_text)  # result header

    def test_result_view_renders_each_label_correctly(self) -> None:
        for label, expected_header in (
            (RestoreResultLabel.VERIFIED, "Verified"),
            (RestoreResultLabel.RESTORED_WITH_WARNINGS, "warnings"),
            (RestoreResultLabel.PARTIALLY_RESTORED, "Partially"),
            (RestoreResultLabel.MISMATCH_AFTER_RESTORE, "Mismatch"),
            (RestoreResultLabel.RESTORE_FAILED, "failed"),
        ):
            with self.subTest(label=label):
                result = RestoreResult(
                    label=label,
                    attempted=1,
                    wrote_succeeded=1 if label != RestoreResultLabel.RESTORE_FAILED else 0,
                    write_failed=1 if label == RestoreResultLabel.RESTORE_FAILED else 0,
                    verified_matched=0,
                    could_not_verify=0,
                    mismatched=0,
                    fields=(),
                    before_restore_point_id=None,
                    completed_at="2026-05-24T19:05:30Z",
                )
                state = screen.RestorePointsScreenState(
                    view=screen.VIEW_RESULT,
                    selected_rp_id="rp_test",
                    result=result,
                )
                service = _FakeService(valid=[_rp(id="rp_test")])
                shell = _shell_with(service, screen_state=state)
                with _PatchedScreen(shell) as ps:
                    ps.build()
                all_text = " ".join(ps.text_strings())
                self.assertIn(expected_header, all_text)

    def test_result_close_button_returns_to_list(self) -> None:
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=_result(),
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
            for kw in ps.buttons:
                if kw.get("tag") == screen.TAG_RESULT_CLOSE_BUTTON:
                    kw["callback"]()
                    break
        self.assertEqual(shell.restore_points_screen_state.view, screen.VIEW_LIST)
        self.assertIsNone(shell.restore_points_screen_state.result)

    def test_result_view_renders_expected_observed_on_mismatch(self) -> None:
        """Restore-result enrichment: when a per-field outcome is a
        mismatch and carries expected/observed values, the result page
        renders an indented "expected: X, observed: Y" line under that
        field. Avoids the problem local testing surfaced, where the user had to leave
        the result page and click Read to learn what value the device
        actually had.
        """
        result = RestoreResult(
            label=RestoreResultLabel.MISMATCH_AFTER_RESTORE,
            attempted=1,
            wrote_succeeded=1,
            write_failed=0,
            verified_matched=0,
            could_not_verify=0,
            mismatched=1,
            fields=(
                RestoreFieldOutcome(
                    field_name="step_size",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=False,
                    verify_note="read-back value differs from expected",
                    expected_value="131",
                    observed_value="146",
                ),
            ),
            before_restore_point_id=None,
            completed_at="2026-05-25T22:28:17Z",
        )
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=result,
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("step_size", all_text)
        self.assertIn("expected: 131", all_text)
        self.assertIn("observed: 146", all_text)

    def test_result_view_renders_compact_collection_values_on_mismatch(self) -> None:
        """Compact-preview rendering: when a per-field outcome's
        expected/observed values are for a collection-type field
        (vibration, deadzones, ...), :class:`RestoreFieldOutcome` stores
        the compact rendering produced by
        :mod:`zd_app.services.restore_field_formatting` rather than the
        noisy ``str(dataclass)`` form. The screen surfaces those strings
        verbatim, so the user reads "L=10 R=20 TL=30 TR=40 (NATIVE)"
        instead of "VibrationSettings(left_grip_strength=10, ...)".

        Pins the contract by passing the compact strings through
        :class:`RestoreFieldOutcome` directly and asserting the screen
        renders them without ever leaking the class-name prefix.
        """
        result = RestoreResult(
            label=RestoreResultLabel.MISMATCH_AFTER_RESTORE,
            attempted=1,
            wrote_succeeded=1,
            write_failed=0,
            verified_matched=0,
            could_not_verify=0,
            mismatched=1,
            fields=(
                RestoreFieldOutcome(
                    field_name="vibration",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=False,
                    verify_note="read-back value differs from expected",
                    expected_value="L=10 R=20 TL=30 TR=40 (NATIVE)",
                    observed_value="L=99 R=99 TL=99 TR=99 (STEREO_RESONANCE)",
                ),
            ),
            before_restore_point_id=None,
            completed_at="2026-05-26T03:00:00Z",
        )
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=result,
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("vibration", all_text)
        self.assertIn("L=10 R=20 TL=30 TR=40 (NATIVE)", all_text)
        self.assertIn("L=99 R=99 TL=99 TR=99 (STEREO_RESONANCE)", all_text)
        # Critical: the class-name prefix must NOT appear anywhere on the
        # result page — that's the whole motivation for the compact helper.
        self.assertNotIn("VibrationSettings(", all_text)

    def test_result_view_omits_expected_observed_when_values_missing(self) -> None:
        """Mismatched fields without expected_value/observed_value (e.g.
        legacy result records or any future code path that doesn't populate
        the new fields) must NOT crash and must NOT render the
        "expected:/observed:" extra line.
        """
        result = RestoreResult(
            label=RestoreResultLabel.MISMATCH_AFTER_RESTORE,
            attempted=1,
            wrote_succeeded=1,
            write_failed=0,
            verified_matched=0,
            could_not_verify=0,
            mismatched=1,
            fields=(
                RestoreFieldOutcome(
                    field_name="step_size",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=False,
                    verify_note="read-back value differs from expected",
                    # expected_value/observed_value default-None
                ),
            ),
            before_restore_point_id=None,
            completed_at="2026-05-25T22:28:17Z",
        )
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=result,
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("step_size", all_text)
        self.assertNotIn("expected:", all_text)
        self.assertNotIn("observed:", all_text)

    def test_result_view_renders_verify_note_inline_on_unverified(self) -> None:
        """Restore-result enrichment: when a per-field outcome is
        verify=unverified (verify_matched is None) and carries a verify_note
        (e.g. "verify-read failed: TimeoutError: HID read timed out after
        967ms"), the result page appends the reason inline so the user
        doesn't have to grep zd_wrapper.log.
        """
        result = RestoreResult(
            label=RestoreResultLabel.RESTORED_WITH_WARNINGS,
            attempted=1,
            wrote_succeeded=1,
            write_failed=0,
            verified_matched=0,
            could_not_verify=1,
            mismatched=0,
            fields=(
                RestoreFieldOutcome(
                    field_name="polling_rate",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=None,
                    verify_note=(
                        "verify-read failed: TimeoutError: "
                        "HID read timed out after 967ms"
                    ),
                ),
            ),
            before_restore_point_id=None,
            completed_at="2026-05-25T22:28:17Z",
        )
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id="rp_test",
            result=result,
        )
        service = _FakeService(valid=[_rp(id="rp_test")])
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        all_text = " ".join(ps.text_strings())
        self.assertIn("polling_rate", all_text)
        self.assertIn("unverified", all_text)
        self.assertIn("HID read timed out after 967ms", all_text)
        self.assertIn("TimeoutError", all_text)


class ScreenStateTests(unittest.TestCase):
    def test_ensure_state_lazily_initialises_default(self) -> None:
        shell = SimpleNamespace(restore_points_screen_state=None)
        state = screen._ensure_state(shell)
        self.assertIs(state, shell.restore_points_screen_state)
        self.assertEqual(state.view, screen.VIEW_LIST)
        self.assertIsNone(state.selected_rp_id)


# ---------------------------------------------------------------------------
# Restore-point load-by-id-bug regression — real-store integration
# ---------------------------------------------------------------------------


class SafeLoadRealStoreIntegrationTests(unittest.TestCase):
    """End-to-end ``_safe_load`` against a real ``RestorePointStore``.

    The existing screen tests above use ``_FakeService`` which short-circuits
    the load path with an in-memory id->rp dict. That covers the screen's
    state-machine wiring but never exercises the actual JSON-on-disk path
    that broke against the actual on-disk data: list rendered the
    3 on-disk RPs but every View / Restore / Export-JSON button reported the
    target id as missing.

    These tests bind the screen helper to the real production classes
    (:class:`RestorePointStore` + :class:`RestorePointService`) so any future
    regression of the "list works but load-by-id doesn't" symptom would be
    caught at pytest time instead of needing a hardware smoke.
    """

    def _make_real_service(self, tmpdir: str):
        import tempfile  # noqa: F401  (kept for clarity that callers own tmpdir)

        from zd_app.services.restore_point_service import RestorePointService
        from zd_app.storage.restore_point_store import RestorePointStore

        store = RestorePointStore(tmpdir)
        service = RestorePointService(
            store=store,
            settings_service=MagicMock(),
            apply_coordinator=MagicMock(),
            app_version="2.0.0-test",
            app_build_commit="abc1234",
        )
        return service, store

    def test_safe_load_finds_rp_via_real_store_primary_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service, store = self._make_real_service(tmpdir)
            rp = _rp(id="rp_20260524_190355_aaaaaa")
            store.save(rp)
            loaded = screen._safe_load(service, rp.id)
            self.assertIsNotNone(loaded, "_safe_load returned None for a saved RP")
            self.assertEqual(loaded.id, rp.id)
            self.assertEqual(loaded.schema_version, rp.schema_version)

    def test_safe_load_returns_none_for_unknown_id(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service, store = self._make_real_service(tmpdir)
            rp = _rp(id="rp_20260524_190355_bbbbbb")
            store.save(rp)
            self.assertIsNone(screen._safe_load(service, "rp_does_not_exist"))

    def test_view_button_click_flow_loads_rp_against_real_store(self) -> None:
        """Exercise the LIST -> button-click -> DETAIL flow on a real store.

        Mirrors what the user does in the wrapper: render LIST, click View
        on one of the rendered rows, expect the DETAIL view to load the
        target RP instead of bouncing back to LIST with the "unavailable"
        warning.
        """

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service, store = self._make_real_service(tmpdir)
            rp = _rp(id="rp_20260524_190355_cccccc")
            store.save(rp)

            # Render the LIST view to capture button callbacks (matches the
            # wrapper boot path: navigate to Restore Points -> list renders).
            shell = _shell_with(service)
            with _PatchedScreen(shell) as ps:
                ps.build()
                view_callback = None
                for kw in ps.buttons:
                    # The View buttons in _build_list_row don't carry a tag,
                    # so locate them by label.
                    if kw.get("label", "").lower().startswith("view"):
                        view_callback = kw["callback"]
                        break
            self.assertIsNotNone(view_callback, "View button not rendered for saved RP")

            # Click View — should transition to VIEW_DETAIL and not be
            # bounced back to LIST by _build_detail's load-miss guard.
            view_callback()
            state = shell.restore_points_screen_state
            self.assertEqual(state.selected_rp_id, rp.id)

            # Now rebuild as if rebuild_current_screen ran (the actual shell
            # does this in production; the patched mock just records the
            # call). Re-render with the DETAIL view active and confirm it
            # renders the detail page rather than bouncing back to LIST.
            with _PatchedScreen(shell) as ps2:
                ps2.build()
                tags = ps2.button_tags()
            # If _safe_load returned None, _build_detail would call
            # _back_to_list and state.view would flip back to VIEW_LIST.
            self.assertEqual(state.view, screen.VIEW_DETAIL)
            self.assertIn(screen.TAG_DETAIL_RESTORE_BUTTON, tags)
            self.assertIn(screen.TAG_DETAIL_EXPORT_BUTTON, tags)


class ListRowButtonDpgSignatureTests(unittest.TestCase):
    """Regression for the restore-point load-by-id bug (2026-05-24).

    DPG inspects a button callback's signature and invokes it with three
    positional args — ``(sender, app_data, user_data)`` — even when the
    callable's params have default values. The pre-fix lambdas in
    ``_build_list_row`` used the bare default-arg idiom
    (``lambda rp_id=rp_id: _on_view(shell, rp_id)``) which let the integer
    widget tag for ``sender`` override the closure-captured string
    ``rp_id``. The result: ``_on_view(shell, <int>)`` ran, the screen
    stashed the int as ``state.selected_rp_id``, and every downstream
    ``_safe_load`` lookup raised ``FileNotFoundError`` because no JSON file
    on disk has an integer id.

    These tests pin the contract by invoking the lambdas the way DPG does
    (with a fake sender int) and asserting the string ``rp.id`` survives.
    """

    def _capture_row_callbacks(self, service, rp) -> dict[str, object]:
        """Render a LIST row for ``rp`` and return the button callbacks."""

        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
            captured: dict[str, object] = {}
            for kw in ps.buttons:
                label = kw.get("label", "")
                if label.lower().startswith("view"):
                    captured["view"] = kw["callback"]
                elif label.lower().startswith("restore"):
                    captured["restore"] = kw["callback"]
                elif label.lower().startswith("export"):
                    captured["export"] = kw["callback"]
        return captured, shell

    def test_view_button_lambda_survives_dpg_positional_sender(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from zd_app.services.restore_point_service import RestorePointService
            from zd_app.storage.restore_point_store import RestorePointStore

            store = RestorePointStore(tmpdir)
            service = RestorePointService(
                store=store,
                settings_service=MagicMock(),
                apply_coordinator=MagicMock(),
                app_version="2.0.0-test",
                app_build_commit=None,
            )
            rp = _rp(id="rp_20260524_190355_dddddd")
            store.save(rp)

            callbacks, shell = self._capture_row_callbacks(service, rp)
            self.assertIn("view", callbacks)

            # DPG-style invocation: sender is the integer widget tag, app_data
            # is None for buttons, user_data is None. Three positional args.
            # (The pre-fix bare ``rp_id=rp_id`` lambda failed here because the
            # first positional sender_int overrode rp_id.)
            fake_sender = 194  # the actual integer that appeared in the 2026-05-24 repro log
            callbacks["view"](fake_sender, None, None)

            state = shell.restore_points_screen_state
            self.assertEqual(state.selected_rp_id, rp.id)
            self.assertNotEqual(state.selected_rp_id, fake_sender)
            self.assertEqual(state.view, screen.VIEW_DETAIL)

    def test_view_button_lambda_survives_four_positional_args(self) -> None:
        """DPG's actual production click dispatcher passes one positional arg
        per POSITIONAL_OR_KEYWORD param in the callable's signature — NOT a
        fixed three. The original load-by-id bug fix used a 4-param lambda
        (``_s=None, _a=None, _u=None, rp_id=rp_id``) which broke in production
        because DPG passed a 4th None that overrode rp_id with None. The
        correct fix uses ``*args, rp_id=rp_id`` so DPG sees VAR_POSITIONAL and
        passes zero positional args. This test pins the 4+ args contract.
        """

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from zd_app.services.restore_point_service import RestorePointService
            from zd_app.storage.restore_point_store import RestorePointStore

            store = RestorePointStore(tmpdir)
            service = RestorePointService(
                store=store,
                settings_service=MagicMock(),
                apply_coordinator=MagicMock(),
                app_version="2.0.0-test",
                app_build_commit=None,
            )
            rp = _rp(id="rp_20260525_215526_ab12c7")
            store.save(rp)

            callbacks, shell = self._capture_row_callbacks(service, rp)

            # Invoke with FOUR args — what the hardware repro showed
            # DPG actually passes when the callable signature has four
            # POSITIONAL_OR_KEYWORD params. Reverting the fix to the 4-param
            # idiom causes this assertion to fail (rp_id becomes None).
            callbacks["view"](42, None, None, None)
            state = shell.restore_points_screen_state
            self.assertEqual(state.selected_rp_id, rp.id)
            self.assertIsNotNone(state.selected_rp_id)

            # And zero args (covers the path where DPG sees VAR_POSITIONAL):
            shell.restore_points_screen_state.selected_rp_id = None
            callbacks["view"]()
            self.assertEqual(shell.restore_points_screen_state.selected_rp_id, rp.id)

    def test_restore_button_lambda_survives_dpg_positional_sender(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from zd_app.services.restore_point_service import RestorePointService
            from zd_app.storage.restore_point_store import RestorePointStore

            store = RestorePointStore(tmpdir)
            service = RestorePointService(
                store=store,
                settings_service=MagicMock(),
                apply_coordinator=MagicMock(),
                app_version="2.0.0-test",
                app_build_commit=None,
            )
            rp = _rp(id="rp_20260524_190355_eeeeee")
            store.save(rp)

            callbacks, shell = self._capture_row_callbacks(service, rp)
            self.assertIn("restore", callbacks)

            fake_sender = 273
            callbacks["restore"](fake_sender, None, None)

            state = shell.restore_points_screen_state
            self.assertEqual(state.selected_rp_id, rp.id)
            self.assertNotEqual(state.selected_rp_id, fake_sender)
            self.assertEqual(state.view, screen.VIEW_CONFIRM)

    def test_export_button_lambda_survives_dpg_positional_sender(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from zd_app.services.restore_point_service import RestorePointService
            from zd_app.storage.restore_point_store import RestorePointStore

            store = RestorePointStore(tmpdir)
            service = RestorePointService(
                store=store,
                settings_service=MagicMock(),
                apply_coordinator=MagicMock(),
                app_version="2.0.0-test",
                app_build_commit=None,
            )
            rp = _rp(id="rp_20260524_190355_ffffff")
            store.save(rp)

            # _restore_points_export_dir(shell) honors
            # ``shell.restore_points_export_dir_override`` first, then falls
            # back to ``_default_user_data_dir() / "restore_points_exports"``.
            # Setting the override redirects the write under tmpdir so the
            # test doesn't pollute the cwd's ``zd_data/`` tree.
            export_dir = Path(tmpdir) / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)

            shell = _shell_with(service)
            shell.restore_points_export_dir_override = str(export_dir)
            with _PatchedScreen(shell) as ps:
                ps.build()
                export_callback = None
                for kw in ps.buttons:
                    if kw.get("label", "").lower().startswith("export"):
                        export_callback = kw["callback"]
                        break
            self.assertIsNotNone(export_callback, "Export button not rendered")

            fake_sender = 389  # the actual integer that appeared in the 2026-05-24 repro log
            export_callback(fake_sender, None, None)

            state = shell.restore_points_screen_state
            # Export does NOT navigate; it writes a file and refreshes the
            # current screen. The success path leaves status_kind = "info"
            # and writes a JSON file under export_dir; failure (load-by-id
            # broken) leaves status_kind = "warn" with "not found" reason.
            self.assertEqual(
                state.status_kind, "info",
                f"Export reported failure: status_text={state.status_text!r}",
            )
            exported_files = list(export_dir.glob("*.json"))
            self.assertEqual(
                len(exported_files), 1,
                f"Expected one exported JSON; got {exported_files!r}",
            )
            self.assertEqual(exported_files[0].name, f"{rp.id}.json")

    def test_export_refuses_traversal_id_containment(self) -> None:
        # Defense-in-depth: the load-boundary id check normally guarantees a safe
        # stem, but if an rp_id ever carried a traversal path the export writer
        # must refuse rather than write outside the export dir.
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = Path(tmpdir) / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            traversal_id = "../escape"  # resolves to <tmpdir>/escape.json
            service = _FakeService(valid=[_rp(id=traversal_id)])
            shell = _shell_with(service)
            shell.restore_points_export_dir_override = str(export_dir)

            screen._on_export_rp(shell, traversal_id)

            state = shell.restore_points_screen_state
            self.assertEqual(state.status_kind, "warn")
            # Nothing was written: not the escaped path, not inside the export dir.
            self.assertFalse((Path(tmpdir) / "escape.json").exists())
            self.assertEqual(list(export_dir.glob("*.json")), [])


class OtherCardHeightClippingTests(unittest.TestCase):
    """Regressions for the DPG card-height review follow-up.

    The list-row clipping bug (now obsolete — the list is now a
    ``dpg.table``, see ``ListTableStructureTests``) was the surface
    observed on real hardware; this audit covered every OTHER
    fixed-height ``dpg.child_window`` rendered by the Restore Points
    screen. Findings, all driven by the same theme math
    (``ItemSpacing=8`` + ``WindowPadding=16``, text rows ~13px, button
    rows ~25px):

    - **Skipped footer (LIST view)** — the hand-computed
      height was replaced entirely with a ``card()`` that autosizes to content
      (no fixed height), so the collapsed toggle and expanded rows can no
      longer be clipped. The two skipped-footer tests below now pin that
      autosize contract instead of a pixel floor.
    - **Detail identity card** — pre-fix ``height=160`` still clipped the
      lower rows by 38px once a multi-line ``reason`` wrapped (real-viewport
      probe, tools/diag_dpg_card_clip.py). Now fits via DPG-2.x
      ``auto_resize_y`` (legacy fill flag suppressed) — no magic number.
    - **Result counts card** — pre-fix ``height=160`` clipped the lower
      counts by 72px at the shipped fonts; now fits via ``auto_resize_y`` too.
    - **Category / will / wont / result-fields cards** — these shared the
      same hand-computed ``45 + 21*N`` height (itself a fix for an earlier
      ``30 + 18*N`` under-count that clipped the last row). They now use
      autosizing :func:`~zd_app.ui.components.card` panels, so they fit their
      content at any N / locale and can no longer clip a trailing row. The two
      tests below pin that autosize contract — DETAIL view now carries no
      fixed-height child window at all.
    """

    def test_skipped_footer_collapsed_is_autosizing_card_with_toggle(self) -> None:
        # The skipped-files disclosure is a card() that
        # autosizes (height=None) — no magic number to clip the toggle.
        skipped = [
            SkippedFile(path="C:/temp/rp_bad_1.json", error="corrupt"),
        ]
        service = _FakeService(valid=[], skipped=skipped)
        # Default state: skipped_expanded = False
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        sized = [kw.get("height") for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            sized, [],
            f"LIST view must not pin a fixed child_window height anymore "
            f"(card() autosizes to content); got heights {sized}.",
        )
        self.assertIn(screen.TAG_LIST_SKIPPED_TOGGLE, ps.button_tags())
        all_text = " ".join(ps.text_strings())
        self.assertIn("could not be read", all_text)

    def test_skipped_footer_expanded_lists_every_skipped_entry(self) -> None:
        skipped = [
            SkippedFile(path=f"C:/temp/rp_bad_{i}.json", error="corrupt")
            for i in range(5)
        ]
        service = _FakeService(valid=[], skipped=skipped)
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_LIST,
            skipped_expanded=True,
        )
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # Still no fixed height: the card grows with content, so every skipped
        # entry stays visible without an inner scrollbar at any N.
        sized = [kw.get("height") for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            sized, [],
            f"Expanded skipped card must autosize (no fixed height); got "
            f"heights {sized}.",
        )
        all_text = " ".join(ps.text_strings())
        for i in range(5):
            self.assertIn(
                f"rp_bad_{i}.json", all_text,
                f"Expanded skipped card dropped entry rp_bad_{i}.json",
            )

    def test_detail_identity_card_fits_content_no_fixed_height(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_DETAIL, selected_rp_id=rp.id
        )
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The identity card now FITS its content (auto_resize_y) instead of
        # pinning the old 160: a real-viewport probe found that 160 still clipped
        # the lower rows by 38px once a multi-line reason wrapped
        # (tools/diag_dpg_card_clip.py). It is the only auto_resize_y child here.
        fit_cards = [
            kw for kw in ps.child_windows if kw.get("auto_resize_y") is True
        ]
        self.assertEqual(
            len(fit_cards), 1,
            f"Expected exactly one content-fit (auto_resize_y) identity card; "
            f"got {fit_cards!r}",
        )
        self.assertIsNone(
            fit_cards[0].get("height"),
            "Identity card should fit content, not pin a fixed height.",
        )
        self.assertFalse(
            fit_cards[0].get("autosize_y", False),
            "Identity card must suppress the legacy fill flag.",
        )

    def test_detail_category_and_will_wont_cards_autosize(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_DETAIL, selected_rp_id=rp.id
        )
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # ``_coverage()`` returns 6 fields across 4 categories: DEVICE (2),
        # FEEL (1: deadzones), COSMETIC (2: vibration + lighting, per the
        # 2026-06-10 category decision), UNSUPPORTED (1) → 4 category cards. Plus a
        # will-restore card and a wont-restore card = 6 content cards, all
        # autosizing card() panels. The identity card now fits via auto_resize_y
        # too, so DETAIL view pins NO fixed card height anymore.
        sized = [kw.get("height") for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            sized, [],
            f"DETAIL view should pin no fixed card heights now — the identity "
            f"card fits via auto_resize_y and the category / will / wont cards "
            f"autosize via card(). Got {sized}.",
        )
        # card() emits border=True + autosize_y child windows; the screen's
        # root container is border=False so it is not counted here.
        autosize_cards = [
            kw
            for kw in ps.child_windows
            if kw.get("height") is None
            and kw.get("autosize_y")
            and kw.get("border")
        ]
        self.assertEqual(
            len(autosize_cards), 6,
            f"Expected 4 category cards + will + wont = 6 autosizing card() "
            f"panels; got {len(autosize_cards)}: {autosize_cards!r}",
        )
        # will/wont keep their intentional fixed width (two side-by-side
        # columns); the category cards fill the parent (width=-1).
        fixed_width = sorted(
            kw.get("width") for kw in autosize_cards if kw.get("width") == 440
        )
        self.assertEqual(
            fixed_width, [440, 440],
            f"will + wont cards should keep width=440 (real two-column "
            f"layout); got widths "
            f"{[kw.get('width') for kw in autosize_cards]!r}",
        )

    def test_result_field_details_card_autosizes(self) -> None:
        rp = _rp()
        # Multi-field result triggers the field-details card.
        result = RestoreResult(
            label=RestoreResultLabel.VERIFIED,
            attempted=3,
            wrote_succeeded=3,
            write_failed=0,
            verified_matched=3,
            could_not_verify=0,
            mismatched=0,
            fields=(
                RestoreFieldOutcome(
                    field_name="polling_rate",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=True,
                ),
                RestoreFieldOutcome(
                    field_name="step_size",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=True,
                ),
                RestoreFieldOutcome(
                    field_name="vibration",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=True,
                ),
            ),
            before_restore_point_id=None,
            completed_at="2026-05-24T19:05:30Z",
        )
        service = _FakeService(valid=[rp], restore_result=result)
        state = screen.RestorePointsScreenState(
            view=screen.VIEW_RESULT,
            selected_rp_id=rp.id,
            result=result,
        )
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # RESULT view pins NO fixed card height anymore: the 6-row counts card
        # and the per-field details card both fit their content via the DPG-2.x
        # auto_resize_y flag. A real-viewport probe found the prior fixed 160
        # counts card clipped by 72px and the bare card() details panel (legacy
        # autosize_y FILLS on DPG 2.x) scrolled the lower fields out of view by
        # 85px (tools/diag_dpg_card_clip.py).
        sized = [kw.get("height") for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            sized, [],
            f"RESULT view should pin no fixed card heights now (counts +"
            f" field-details both fit via auto_resize_y). Got {sized}.",
        )
        # Both result cards render as content-fit (auto_resize_y) bordered
        # panels with the legacy fill flag suppressed.
        fit_cards = [
            kw
            for kw in ps.child_windows
            if kw.get("auto_resize_y") is True and kw.get("border")
        ]
        self.assertGreaterEqual(
            len(fit_cards), 2,
            f"Expected the counts + field-details cards to fit via "
            f"auto_resize_y; got {fit_cards!r}",
        )
        for kw in fit_cards:
            self.assertFalse(
                kw.get("autosize_y", False),
                f"Result card must suppress the legacy fill flag: {kw}",
            )


class ListTableStructureTests(unittest.TestCase):
    """The saved-restore-point list renders as one native
    ``dpg.table`` inside a ``card()``, replacing the prior
    one-``child_window``-per-RP stack.

    The old stack's recurring failure was the View/Restore/Export button row
    being clipped by a guessed fixed card height (``92`` @ 2026-05-26, ``150``
    @ 2026-05-29 — Restore became reachable only by scrolling inside a card
    that didn't look scrollable). A table sidesteps that whole class of bug:
    rows auto-fit their content and the actions live in a fixed-width Actions
    column so the buttons can never be horizontally clipped. These tests pin
    the table contract — any revert to per-row fixed-height child_windows, a
    dropped column, or a dropped action button fails here.
    """

    def test_list_renders_as_a_table(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertEqual(
            len(ps.tables), 1,
            f"Expected exactly one dpg.table in LIST view; got {len(ps.tables)}.",
        )
        table_kw = ps.tables[0]
        self.assertEqual(table_kw.get("tag"), screen.TAG_LIST_TABLE)
        self.assertTrue(table_kw.get("header_row"), "table needs a header row")
        self.assertTrue(
            table_kw.get("row_background"),
            "table should use alternating row backgrounds (roadmap consensus #5)",
        )

    def test_list_has_no_positive_fixed_height_card(self) -> None:
        # The whole point of table-ifying: no widget pins a POSITIVE fixed pixel
        # height, so the View/Restore/Export buttons can't be clipped below the
        # fold like the pre-table cards were (92 @ 2026-05-26, 150 @ 2026-05-29).
        # A NEGATIVE height is allowed and expected: it is the fill-height footer
        # reserve (see test_list_table_card_reserves_footer) that makes the table
        # card the single scroll surface, not a per-row clip.
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        positive = [
            kw.get("height")
            for kw in ps.child_windows
            if isinstance(kw.get("height"), int) and kw.get("height") > 0
        ]
        self.assertEqual(
            positive, [],
            f"LIST view must not pin a POSITIVE fixed child_window height (table "
            f"rows auto-fit; a clip would hide the action buttons); got {positive}.",
        )

    def test_list_table_card_reserves_footer_for_single_scroll(self) -> None:
        # Scroll discipline: with valid points and no skipped files, the table
        # card fills the height remaining below the top controls MINUS the footer
        # reserve (negative height = ImGui fill-minus), so it scrolls INTERNALLY
        # and the page (autosize_y root) does NOT grow a second scrollbar. Pin the
        # exact reserve so a revert to the autosize_y fill (which put the caveat
        # below the filling card and produced the two-scrollbar bug) fails here.
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        heights = [kw.get("height") for kw in ps.child_windows]
        self.assertIn(
            -screen.FOOTER_RESERVE_PX, heights,
            f"LIST table card must reserve the footer via height="
            f"-{screen.FOOTER_RESERVE_PX} so it is the single scroll surface; "
            f"got child_window heights {heights}.",
        )
        # And the pinned caveat must still render (visible without a page scroll).
        self.assertIn(
            screen.TAG_LIST_FOOTER_CAVEAT,
            [kw.get("tag") for _fn, _a, kw in ps.calls if _fn == "add_text"],
            "LIST view dropped the pinned footer caveat.",
        )

    def test_list_with_skipped_uses_content_fit_not_footer_reserve(self) -> None:
        # Degraded path (valid points AND skipped/corrupt files): the bottom
        # carries a variable-height disclosure card, so the table can't reserve a
        # fixed footer band. It is content-fit instead (auto_resize_y, no fixed
        # height) and the whole page scrolls as ONE bar. Assert: no footer-reserve
        # height, no positive fixed height, and the caveat still renders.
        rp = _rp()
        skipped = [SkippedFile(path="C:/temp/rp_bad_1.json", error="corrupt")]
        service = _FakeService(valid=[rp], skipped=skipped)
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        heights = [kw.get("height") for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(
            heights, [],
            f"skipped-files LIST path must be content-fit (no fixed/negative "
            f"child_window height); got {heights}.",
        )
        fit_cards = [kw for kw in ps.child_windows if kw.get("auto_resize_y") is True]
        self.assertGreaterEqual(
            len(fit_cards), 2,
            "expected the table card AND the skipped card to be content-fit.",
        )
        self.assertIn(
            screen.TAG_LIST_FOOTER_CAVEAT,
            [kw.get("tag") for _fn, _a, kw in ps.calls if _fn == "add_text"],
        )

    def test_list_table_columns_cover_metadata_and_actions(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = [kw.get("label") for kw in ps.table_columns]
        # English literals (the suite runs in the default en locale, like the
        # "Captured 8 of 13" assertion above) — pins the column set + order.
        self.assertEqual(
            labels,
            ["Restore point", "Created", "Coverage", "Last restored", "Actions"],
        )
        # The Actions column is fixed-width so the three buttons never clip —
        # the sizing failure that hid Restore on hardware can't recur here.
        actions_col = ps.table_columns[-1]
        self.assertTrue(
            actions_col.get("width_fixed"),
            "Actions column must be fixed-width to guarantee button reachability.",
        )

    def test_list_row_renders_all_four_action_buttons(self) -> None:
        # A refactor that drops View / Restore / Export / Delete must fail a test.
        rp = _rp()
        service = _FakeService(valid=[rp])
        shell = _shell_with(service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = [kw.get("label", "").lower() for kw in ps.buttons]
        for action in ("view", "restore", "export", "delete"):
            self.assertTrue(
                any(lbl.startswith(action) for lbl in labels),
                f"List row is missing the {action!r} action button; "
                f"button labels rendered: {labels}",
            )


class ListRowDeleteFlowTests(unittest.TestCase):
    """Per-row Delete with a confirm modal (previously there was no
    way to remove an individual restore point from the UI even though
    ``RestorePointStore.delete`` existed).

    The Delete button opens a localized confirm modal; Confirm calls
    ``service.delete(rp_id)`` and refreshes the list with a success / warn
    status, Cancel just closes the modal. The row + modal confirm callbacks
    use the same load-bearing ``lambda *args, ...=...`` idiom as the other
    row buttons (see :class:`ListRowButtonDpgSignatureTests`), so the tests
    invoke them DPG-style with positional sender args.
    """

    def _open_list(self, service) -> _PatchedScreen:
        shell = _shell_with(service)
        ps = _PatchedScreen(shell).__enter__()
        ps.build()
        return ps

    def _click_delete_row_button(self, ps: _PatchedScreen) -> None:
        delete_cb = None
        for kw in ps.buttons:
            if kw.get("label", "").lower().startswith("delete"):
                delete_cb = kw["callback"]
                break
        self.assertIsNotNone(delete_cb, "Delete button not rendered for saved RP")
        # DPG-style invocation with four positional args — the *args idiom
        # must let the closure-captured rp_id/title survive (cf. the
        # signature-regression tests above).
        delete_cb(194, None, None, None)

    def _modal_button(self, ps: _PatchedScreen, tag: str) -> dict:
        for kw in ps.buttons:
            if kw.get("tag") == tag:
                return kw
        self.fail(f"modal button {tag!r} not rendered; buttons: {ps.buttons!r}")

    def test_delete_button_opens_confirm_modal_with_title(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_list(service)
        try:
            self._click_delete_row_button(ps)
            all_text = " ".join(ps.text_strings())
        finally:
            ps.__exit__(None, None, None)
        self.assertEqual(
            len(ps.windows), 1,
            f"Expected the confirm modal window; got {ps.windows!r}",
        )
        modal_kw = ps.windows[0]
        self.assertEqual(modal_kw.get("tag"), screen.TAG_DELETE_CONFIRM_MODAL)
        self.assertTrue(modal_kw.get("modal"), "confirm dialog must be modal")
        self.assertEqual(modal_kw.get("label"), "Delete restore point?")
        # Body names the restore point and warns about irreversibility.
        self.assertIn(rp.title, all_text)
        self.assertIn("cannot be undone", all_text)
        # Opening the modal must not touch the service yet.
        self.assertNotIn(
            "delete", [name for name, _args, _kw in service.calls],
        )

    def test_confirm_calls_service_delete_and_refreshes_with_status(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_list(service)
        try:
            self._click_delete_row_button(ps)
            deletes_before_confirm = len(ps.deleted_items)
            confirm_kw = self._modal_button(
                ps, screen.TAG_DELETE_CONFIRM_CONFIRM_BUTTON
            )
            confirm_kw["callback"](273, None, None)
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(("delete", (rp.id,), {}), service.calls)
        state = ps.shell.restore_points_screen_state
        self.assertEqual(state.status_kind, "info")
        self.assertIn(rp.title, state.status_text)
        ps.shell.rebuild_current_screen.assert_called_once()
        # Confirm closes the modal (exactly one new delete_item, targeting it).
        self.assertEqual(
            ps.deleted_items[deletes_before_confirm:],
            [screen.TAG_DELETE_CONFIRM_MODAL],
        )

    def test_cancel_closes_modal_and_deletes_nothing(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp])
        ps = self._open_list(service)
        try:
            self._click_delete_row_button(ps)
            deletes_before_cancel = len(ps.deleted_items)
            cancel_kw = self._modal_button(
                ps, screen.TAG_DELETE_CONFIRM_CANCEL_BUTTON
            )
            cancel_kw["callback"](42, None, None)
        finally:
            ps.__exit__(None, None, None)
        self.assertNotIn(
            "delete", [name for name, _args, _kw in service.calls],
        )
        self.assertEqual(
            ps.deleted_items[deletes_before_cancel:],
            [screen.TAG_DELETE_CONFIRM_MODAL],
        )
        # No vault change — no rebuild, no status line.
        ps.shell.rebuild_current_screen.assert_not_called()
        self.assertEqual(ps.shell.restore_points_screen_state.status_text, "")

    def test_delete_failure_sets_warn_status(self) -> None:
        rp = _rp()
        service = _FakeService(valid=[rp], delete_returns=False)
        ps = self._open_list(service)
        try:
            self._click_delete_row_button(ps)
            confirm_kw = self._modal_button(
                ps, screen.TAG_DELETE_CONFIRM_CONFIRM_BUTTON
            )
            confirm_kw["callback"](273, None, None)
        finally:
            ps.__exit__(None, None, None)
        self.assertIn(("delete", (rp.id,), {}), service.calls)
        state = ps.shell.restore_points_screen_state
        self.assertEqual(state.status_kind, "warn")
        self.assertIn(rp.title, state.status_text)
        ps.shell.rebuild_current_screen.assert_called_once()

    def test_confirmed_delete_without_service_warns_unavailable(self) -> None:
        # Defensive path: the service vanished between modal-open and confirm
        # (mirrors _on_export_rp's guard). Direct call — no DPG needed beyond
        # the patched delete_item/does_item_exist.
        shell = _shell_with(None)
        with _PatchedScreen(shell):
            screen._on_delete_confirmed(shell, "rp_x", "Title X")
        state = shell.restore_points_screen_state
        self.assertEqual(state.status_kind, "warn")
        self.assertNotEqual(state.status_text, "")
        shell.rebuild_current_screen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
