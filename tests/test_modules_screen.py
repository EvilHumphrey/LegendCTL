"""DPG-free tests for the Modules screen.

Patches the ``dpg`` module on :mod:`zd_app.ui.screens.modules` so ``build()``
can be exercised without a real DPG context. Tests cover the list view
rendering (empty vs assigned card), the detail view (sparkline +
fingerprint expand), the assign / notes / swap flows through the
sanitised helpers, the wizard state transitions, and i18n parity.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional
from unittest.mock import MagicMock, patch

from datetime import datetime, timedelta, timezone

from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.module_passport import (
    CharacterizationOrchestrator,
    CharacterizationState,
    ModulePassportService,
    TREND_STATUS_DRIFTING,
    TREND_STATUS_INSUFFICIENT,
    TREND_STATUS_INVESTIGATE,
    TREND_STATUS_STABLE,
)
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    DIAGNOSTIC_BUNDLE_GENERATED,
    MODULE_TREND_FLAGGED,
)
from zd_app.storage.module_passport_models import (
    ModuleFingerprint,
    ModulePassport,
    SIDE_LEFT,
    SIDE_RIGHT,
    STATUS_GOOD,
    STATUS_WATCH,
    STATUS_WEAR_OBSERVED,
)
from zd_app.ui.screens import modules as modules_screen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fingerprint(**overrides) -> ModuleFingerprint:
    base = dict(
        timestamp_utc="2026-05-26T18:42:11Z",
        side=SIDE_LEFT,
        duration_ms=60_000,
        samples_count=12_000,
        noise_floor_percent=1.4,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=94.0,
        outer_deadzone_max_axis=98.0,
        asymmetry_score=0.05,
        bitness_observed=128,
        tremor_metric=0.2,
        linearity_score=0.04,
        overall_status=STATUS_GOOD,
        notes=None,
    )
    base.update(overrides)
    return ModuleFingerprint(**base)


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _FakeGroup(*_args, **_kw):
    return _FakeContextManager()


class _PatchedScreen:
    """Captures DPG calls so build() runs without a real DPG context."""

    def __init__(self, shell) -> None:
        self.shell = shell
        self.values: dict[str, object] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.buttons: list[dict] = []
        self.combos: list[dict] = []
        self.windows: list[dict] = []
        self._cm = None

    def __enter__(self) -> "_PatchedScreen":
        def record(name):
            def _fn(*args, **kw):
                self.calls.append((name, args, kw))
                if name == "add_button":
                    self.buttons.append(kw)
                if name == "add_combo":
                    self.combos.append(kw)
                if name == "add_input_text":
                    self.values[kw.get("tag", "")] = kw.get("default_value", "")
                return kw.get("tag", name)

            return _fn

        def record_child_window(*args, **kw):
            self.calls.append(("child_window", args, kw))
            return _FakeContextManager()

        def record_window(*args, **kw):
            self.calls.append(("window", args, kw))
            self.windows.append(kw)
            return _FakeContextManager()

        def set_value(tag, value):
            self.values[tag] = value

        def get_value(tag):
            return self.values.get(tag, "")

        self._cm = patch.multiple(
            "zd_app.ui.screens.modules.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_combo=record("add_combo"),
            add_input_text=record("add_input_text"),
            add_progress_bar=record("add_progress_bar"),
            child_window=record_child_window,
            window=record_window,
            group=_FakeGroup,
            set_value=set_value,
            get_value=get_value,
            does_item_exist=lambda *_a, **_kw: False,
            delete_item=lambda *_a, **_kw: None,
            get_frame_count=lambda: 0,
            set_frame_callback=lambda *_a, **_kw: None,
        )
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False

    def build(self) -> None:
        modules_screen.build(self.shell, parent="content_region")

    def text_strings(self) -> list[str]:
        out: list[str] = []
        for fn, args, kw in self.calls:
            if fn == "add_text":
                if args:
                    out.append(str(args[0]))
                elif "default_value" in kw:
                    out.append(str(kw["default_value"]))
        return out

    def button_labels(self) -> list[str]:
        return [str(kw.get("label", "")) for kw in self.buttons]


def _shell_with(
    service: Optional[ModulePassportService],
    *,
    state: Optional[modules_screen.ModulesScreenState] = None,
    diagnostic_bundle_service: Optional[DiagnosticBundleService] = None,
    device_service: Optional[object] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        module_passport_service=service,
        diagnostic_bundle_service=diagnostic_bundle_service,
        device_service=device_service,
        modules_screen_state=state,
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


# ---------------------------------------------------------------------------
# Unavailable / empty states
# ---------------------------------------------------------------------------


class UnavailableServiceTests(unittest.TestCase):
    def test_renders_unavailable_text_when_service_is_none(self) -> None:
        shell = _shell_with(None)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertIn("unavailable", joined)


class EmptyListTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_empty_state_shows_both_card_titles_and_assign_buttons(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        # Two "Assign module" buttons (one per side).
        assign_count = sum(1 for lbl in labels if "Assign module" == lbl)
        self.assertEqual(assign_count, 2)


# ---------------------------------------------------------------------------
# List view with passports assigned
# ---------------------------------------------------------------------------


class ListViewWithDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = WearLedgerService(base_dir=Path(self._tmp.name) / "ledger")
        self.service = ModulePassportService(
            base_dir=Path(self._tmp.name) / "passports",
            wear_ledger=self.ledger,
        )

    def _seed_left(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT", notes="baseline")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint())

    def test_assigned_card_shows_module_id_and_status(self) -> None:
        self._seed_left()
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("STOCK_LEFT", joined)
        # Status label for STATUS_GOOD ("Good") rendered for the latest run.
        self.assertIn("Good", joined)

    def test_assigned_card_shows_run_button(self) -> None:
        self._seed_left()
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The "Run characterization" button label includes the duration hint.
        run_buttons = [lbl for lbl in ps.button_labels() if "Run characterization" in lbl]
        self.assertEqual(len(run_buttons), 1)


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------


class DetailViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))
        self.service.assign(SIDE_LEFT, "STOCK_LEFT", notes="some notes")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.service.append_fingerprint(
            SIDE_LEFT,
            _fingerprint(timestamp_utc="2026-05-27T10:00:00Z", overall_status=STATUS_WATCH),
        )

    def test_detail_view_shows_history_section(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Characterization history", joined)
        # Both fingerprint timestamps render.
        self.assertIn("2026-05-26T18:42:11Z", joined)
        self.assertIn("2026-05-27T10:00:00Z", joined)

    def test_detail_view_renders_sparkline_squares(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The sparkline emits one ■ per fingerprint; per-row indicators
        # also emit one ■ each. With 2 fingerprints, expect at least 2
        # squares (sparkline) plus the per-row glyphs.
        squares = [
            args for fn, args, kw in ps.calls if fn == "add_text" and args and args[0] == "■"
        ]
        self.assertGreaterEqual(len(squares), 2)

    def test_detail_with_no_history_shows_empty_message(self) -> None:
        # Wipe history for a clean check.
        with tempfile.TemporaryDirectory() as fresh_tmp:
            fresh_service = ModulePassportService(base_dir=Path(fresh_tmp))
            fresh_service.assign(SIDE_LEFT, "FRESH")
            state = modules_screen.ModulesScreenState(
                view=modules_screen.VIEW_DETAIL,
                detail_side=SIDE_LEFT,
            )
            shell = _shell_with(fresh_service, state=state)
            with _PatchedScreen(shell) as ps:
                ps.build()
            joined = "\n".join(ps.text_strings())
            self.assertIn("No characterization runs", joined)

    def test_toggle_fingerprint_expand_updates_state(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        modules_screen._on_toggle_fingerprint(shell, 0)
        self.assertEqual(state.expanded_fingerprint_idx, 0)
        # Toggling the same index again collapses.
        modules_screen._on_toggle_fingerprint(shell, 0)
        self.assertIsNone(state.expanded_fingerprint_idx)
        shell.rebuild_current_screen.assert_called()


# ---------------------------------------------------------------------------
# Assign / save helper
# ---------------------------------------------------------------------------


class AssignHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_save_assign_creates_passport(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(self.service, state=state)
        passport = modules_screen._save_assign(
            shell, SIDE_LEFT, "STOCK_LEFT", "baseline"
        )
        self.assertIsNotNone(passport)
        assert passport is not None
        self.assertEqual(passport.module_id, "STOCK_LEFT")
        self.assertEqual(state.status_text, "")

    def test_save_assign_empty_id_warns(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(self.service, state=state)
        result = modules_screen._save_assign(shell, SIDE_LEFT, "  \n  ", "")
        self.assertIsNone(result)
        self.assertNotEqual(state.status_text, "")
        self.assertEqual(state.status_kind, "warn")

    def test_save_assign_service_unavailable_warns(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(None, state=state)
        result = modules_screen._save_assign(shell, SIDE_LEFT, "ID", "")
        self.assertIsNone(result)
        self.assertEqual(state.status_kind, "warn")


# ---------------------------------------------------------------------------
# Wizard state transitions through screen callbacks
# ---------------------------------------------------------------------------


class WizardCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")

    def test_on_start_wizard_no_passport_warns(self) -> None:
        state = modules_screen.ModulesScreenState()
        empty_service = ModulePassportService(
            base_dir=Path(self._tmp.name) / "empty"
        )
        shell = _shell_with(empty_service, state=state)
        modules_screen._on_start_wizard(shell, SIDE_RIGHT)
        self.assertEqual(state.view, modules_screen.VIEW_LIST)
        self.assertEqual(state.status_kind, "warn")

    def test_on_start_wizard_creates_orchestrator(self) -> None:
        state = modules_screen.ModulesScreenState(
            sample_provider_factory=lambda: (lambda: None),
        )
        shell = _shell_with(self.service, state=state)
        modules_screen._on_start_wizard(shell, SIDE_LEFT)
        self.assertEqual(state.view, modules_screen.VIEW_WIZARD)
        self.assertIsNotNone(state.orchestrator)
        assert state.orchestrator is not None
        self.assertEqual(state.orchestrator.state, CharacterizationState.READY_REST)
        # Tidy up the daemon collector thread.
        state.orchestrator.cancel()

    def test_on_wizard_save_appends_fingerprint(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(self.service, state=state)
        # Pre-build an orchestrator in COMPLETE state with a known fingerprint.
        orchestrator = CharacterizationOrchestrator(
            side=SIDE_LEFT,
            sample_provider=lambda: None,
        )
        # Inject the fingerprint directly to skip the wall-clock walk.
        orchestrator._fingerprint = _fingerprint()
        orchestrator._state = CharacterizationState.COMPLETE
        state.orchestrator = orchestrator
        state.view = modules_screen.VIEW_WIZARD
        modules_screen._on_wizard_save(shell)
        passport = self.service.get(SIDE_LEFT)
        assert passport is not None
        self.assertEqual(len(passport.fingerprints), 1)
        self.assertEqual(state.view, modules_screen.VIEW_LIST)
        self.assertIsNone(state.orchestrator)

    def test_on_wizard_discard_resets(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(self.service, state=state)
        orchestrator = CharacterizationOrchestrator(
            side=SIDE_LEFT,
            sample_provider=lambda: None,
        )
        orchestrator._state = CharacterizationState.CANCELLED
        state.orchestrator = orchestrator
        state.view = modules_screen.VIEW_WIZARD
        modules_screen._on_wizard_discard(shell)
        self.assertIsNone(state.orchestrator)
        self.assertEqual(state.view, modules_screen.VIEW_LIST)


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


class NavigationCallbackTests(unittest.TestCase):
    def test_view_detail_sets_state(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(None, state=state)
        modules_screen._on_view_detail(shell, SIDE_RIGHT)
        self.assertEqual(state.view, modules_screen.VIEW_DETAIL)
        self.assertEqual(state.detail_side, SIDE_RIGHT)

    def test_back_to_list_resets_detail(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
            expanded_fingerprint_idx=2,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_back_to_list(shell)
        self.assertEqual(state.view, modules_screen.VIEW_LIST)
        self.assertIsNone(state.detail_side)
        self.assertIsNone(state.expanded_fingerprint_idx)

    def test_back_to_list_clears_archived_and_compare_state(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=2,
            archived_expanded_fingerprint_idx=1,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_back_to_list(shell)
        self.assertEqual(state.view, modules_screen.VIEW_LIST)
        self.assertIsNone(state.archived_side)
        self.assertIsNone(state.archived_detail_index)
        self.assertIsNone(state.archived_expanded_fingerprint_idx)

    def test_open_archived_list_sets_state(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(None, state=state)
        modules_screen._on_open_archived_list(shell, SIDE_RIGHT)
        self.assertEqual(state.view, modules_screen.VIEW_ARCHIVED_LIST)
        self.assertEqual(state.archived_side, SIDE_RIGHT)
        self.assertIsNone(state.archived_detail_index)

    def test_open_archived_detail_sets_index(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_LIST,
            archived_side=SIDE_LEFT,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_open_archived_detail(shell, 3)
        self.assertEqual(state.view, modules_screen.VIEW_ARCHIVED_DETAIL)
        self.assertEqual(state.archived_detail_index, 3)

    def test_back_to_archived_list_clears_detail_index(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=2,
            archived_expanded_fingerprint_idx=4,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_back_to_archived_list(shell)
        self.assertEqual(state.view, modules_screen.VIEW_ARCHIVED_LIST)
        self.assertIsNone(state.archived_detail_index)
        self.assertIsNone(state.archived_expanded_fingerprint_idx)
        # Archived side stays so the list view re-renders the same side.
        self.assertEqual(state.archived_side, SIDE_LEFT)

    def test_open_compare_sets_view(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_open_compare(shell)
        self.assertEqual(state.view, modules_screen.VIEW_COMPARE)
        self.assertIsNone(state.detail_side)

    def test_toggle_archived_fingerprint(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=0,
        )
        shell = _shell_with(None, state=state)
        modules_screen._on_toggle_archived_fingerprint(shell, 0)
        self.assertEqual(state.archived_expanded_fingerprint_idx, 0)
        modules_screen._on_toggle_archived_fingerprint(shell, 0)
        self.assertIsNone(state.archived_expanded_fingerprint_idx)


# ---------------------------------------------------------------------------
# Archived browser
# ---------------------------------------------------------------------------


class ArchivedListViewTests(unittest.TestCase):
    """ARCHIVED_LIST view renders one row per archived passport.

    Setup creates two archived passports on the LEFT side by assigning
    three modules in sequence: FIRST → archived when SECOND lands, SECOND
    → archived when THIRD lands, leaving THIRD as the active passport.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))
        self.service.assign(SIDE_LEFT, "FIRST", notes="baseline")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.service.assign(SIDE_LEFT, "SECOND", notes="k-silver")
        self.service.assign(SIDE_LEFT, "THIRD")

    def test_list_view_button_hidden_when_no_archive(self) -> None:
        # A side with no archive entries must not show the view-archived
        # button — it would lead to an empty list.
        with tempfile.TemporaryDirectory() as fresh_tmp:
            empty_service = ModulePassportService(base_dir=Path(fresh_tmp))
            empty_service.assign(SIDE_LEFT, "ONLY")
            shell = _shell_with(empty_service)
            with _PatchedScreen(shell) as ps:
                ps.build()
            labels = ps.button_labels()
            self.assertNotIn("View archived passports", labels)

    def test_list_view_button_shown_when_archive_exists(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("View archived passports", labels)

    def test_archived_list_renders_one_row_per_entry(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_LIST,
            archived_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # Two archived passports — FIRST and SECOND.
        self.assertIn("FIRST", joined)
        self.assertIn("SECOND", joined)
        # THIRD is the active passport, not archived.
        self.assertNotIn("THIRD ", joined)  # whitespace ensures "THIRD" prefix doesn't match anything else

    def test_archived_list_empty_state_for_other_side(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_LIST,
            archived_side=SIDE_RIGHT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings()).lower()
        self.assertIn("no archived", joined)

    def test_archived_list_entry_shows_notes_snippet(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_LIST,
            archived_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # Both passports carry notes that should surface in snippet form.
        self.assertIn("baseline", joined)
        self.assertIn("k-silver", joined)

    def test_archived_list_back_button_present(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_LIST,
            archived_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("Back to modules", labels)


class ArchivedDetailViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))
        self.service.assign(SIDE_LEFT, "FIRST", notes="baseline notes")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint())
        self.service.append_fingerprint(
            SIDE_LEFT,
            _fingerprint(timestamp_utc="2026-05-27T10:00:00Z", overall_status=STATUS_WATCH),
        )
        # Archive FIRST.
        self.service.assign(SIDE_LEFT, "SECOND")

    def test_archived_detail_renders_module_id_and_history(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=0,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("FIRST", joined)
        self.assertIn("baseline notes", joined)
        # Both fingerprint timestamps appear in the history.
        self.assertIn("2026-05-26T18:42:11Z", joined)
        self.assertIn("2026-05-27T10:00:00Z", joined)

    def test_archived_detail_back_button_returns_to_list(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=0,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("Back to archive list", labels)

    def test_archived_detail_out_of_range_falls_back_to_list(self) -> None:
        # Index past the end → render the archived LIST view instead of
        # crashing. State is mutated to record the fallback.
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_ARCHIVED_DETAIL,
            archived_side=SIDE_LEFT,
            archived_detail_index=99,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertEqual(state.view, modules_screen.VIEW_ARCHIVED_LIST)
        self.assertIsNone(state.archived_detail_index)


# ---------------------------------------------------------------------------
# Compare view
# ---------------------------------------------------------------------------


class CompareViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_compare_button_renders_on_list_view(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("Compare left + right", labels)

    def test_compare_view_with_both_sides_renders_columns(self) -> None:
        self.service.assign(SIDE_LEFT, "L_MOD", notes="left side")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint(side=SIDE_LEFT))
        self.service.assign(SIDE_RIGHT, "R_MOD", notes="right side")
        self.service.append_fingerprint(SIDE_RIGHT, _fingerprint(side=SIDE_RIGHT))
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("L_MOD", joined)
        self.assertIn("R_MOD", joined)
        # Latest fingerprint heading shows in both columns.
        self.assertEqual(joined.count("Latest fingerprint"), 2)

    def test_compare_view_with_only_left_shows_empty_right(self) -> None:
        self.service.assign(SIDE_LEFT, "L_ONLY")
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("L_ONLY", joined)
        self.assertIn("No right module assigned", joined)

    def test_compare_view_with_only_right_shows_empty_left(self) -> None:
        self.service.assign(SIDE_RIGHT, "R_ONLY")
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("R_ONLY", joined)
        self.assertIn("No left module assigned", joined)

    def test_compare_view_unavailable_delta_when_one_side_lacks_fp(self) -> None:
        self.service.assign(SIDE_LEFT, "L_MOD")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint(side=SIDE_LEFT))
        self.service.assign(SIDE_RIGHT, "R_MOD")
        # Right has no fingerprints — delta unavailable.
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Both sides need", joined)

    def test_compare_view_delta_none_when_metrics_match(self) -> None:
        self.service.assign(SIDE_LEFT, "L_MOD")
        self.service.append_fingerprint(SIDE_LEFT, _fingerprint(side=SIDE_LEFT))
        self.service.assign(SIDE_RIGHT, "R_MOD")
        # Right fingerprint identical metrics → no flags should fire.
        self.service.append_fingerprint(SIDE_RIGHT, _fingerprint(side=SIDE_RIGHT))
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("No notable differences", joined)

    def test_compare_view_delta_flags_render_when_diverge(self) -> None:
        self.service.assign(SIDE_LEFT, "L_MOD")
        # Push left's noise_floor far above the threshold (delta of 2.0%).
        self.service.append_fingerprint(
            SIDE_LEFT, _fingerprint(side=SIDE_LEFT, noise_floor_percent=4.0)
        )
        self.service.assign(SIDE_RIGHT, "R_MOD")
        self.service.append_fingerprint(
            SIDE_RIGHT, _fingerprint(side=SIDE_RIGHT, noise_floor_percent=1.0)
        )
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # The metric name from the localised key must appear in the delta row.
        self.assertIn("Noise floor", joined)
        # Caveat row also renders alongside any flagged metric.
        self.assertIn("Heuristic hints only", joined)

    def test_compare_view_back_button_present(self) -> None:
        state = modules_screen.ModulesScreenState(view=modules_screen.VIEW_COMPARE)
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("Back to modules", labels)


class ComputeDeltaFlagsTests(unittest.TestCase):
    """Pure-function tests for ``_compute_delta_flags`` — no rendering."""

    def test_no_flags_when_identical(self) -> None:
        fp = _fingerprint()
        flags = modules_screen._compute_delta_flags(fp, fp)
        self.assertEqual(flags, [])

    def test_noise_floor_flag_fires_above_threshold(self) -> None:
        left = _fingerprint(noise_floor_percent=2.0)
        right = _fingerprint(noise_floor_percent=1.0)  # delta = 1.0 > 0.5
        flags = modules_screen._compute_delta_flags(left, right)
        self.assertTrue(any(f.metric_key == "noise_floor" for f in flags))

    def test_noise_floor_flag_below_threshold(self) -> None:
        left = _fingerprint(noise_floor_percent=1.1)
        right = _fingerprint(noise_floor_percent=1.0)  # delta = 0.1 < 0.5
        flags = modules_screen._compute_delta_flags(left, right)
        self.assertFalse(any(f.metric_key == "noise_floor" for f in flags))

    def test_bitness_flag_uses_integer_threshold(self) -> None:
        left = _fingerprint(bitness_observed=200)
        right = _fingerprint(bitness_observed=100)  # delta = 100 > 32
        flags = modules_screen._compute_delta_flags(left, right)
        bit_flag = next(f for f in flags if f.metric_key == "bitness")
        self.assertEqual(bit_flag.delta_display, "+100")

    def test_circularity_flag_uses_percent_units(self) -> None:
        # circularity_coverage_percent is stored as a fraction (0..1) but the
        # threshold is in percentage points. 0.85 vs 0.95 = 10pt > 5pt.
        left = _fingerprint(circularity_coverage_percent=0.85)
        right = _fingerprint(circularity_coverage_percent=0.95)
        flags = modules_screen._compute_delta_flags(left, right)
        circ_flag = next(f for f in flags if f.metric_key == "circularity")
        # Display values are in percent — 85.0 vs 95.0.
        self.assertEqual(circ_flag.left_display, "85.0")
        self.assertEqual(circ_flag.right_display, "95.0")

    def test_outer_deadzone_flag_uses_span(self) -> None:
        # span = max - min. Left span 10, right span 2 → delta = 8 > 3.
        left = _fingerprint(outer_deadzone_min_axis=90, outer_deadzone_max_axis=100)
        right = _fingerprint(outer_deadzone_min_axis=97, outer_deadzone_max_axis=99)
        flags = modules_screen._compute_delta_flags(left, right)
        self.assertTrue(any(f.metric_key == "outer_deadzone" for f in flags))

    def test_multiple_flags_fire_in_fixed_order(self) -> None:
        # Push every metric over its threshold.
        left = _fingerprint(
            noise_floor_percent=3.0,
            centering_offset_x=5.0,
            centering_offset_y=0.0,
            circularity_coverage_percent=0.5,
            outer_deadzone_min_axis=80,
            outer_deadzone_max_axis=100,
            asymmetry_score=0.5,
            bitness_observed=400,
            tremor_metric=1.0,
            linearity_score=0.5,
        )
        right = _fingerprint(
            noise_floor_percent=0.5,
            centering_offset_x=0.0,
            centering_offset_y=0.0,
            circularity_coverage_percent=0.99,
            outer_deadzone_min_axis=97,
            outer_deadzone_max_axis=98,
            asymmetry_score=0.01,
            bitness_observed=128,
            tremor_metric=0.1,
            linearity_score=0.01,
        )
        flags = modules_screen._compute_delta_flags(left, right)
        order = [f.metric_key for f in flags]
        # Stable: matches the order asserted in _compute_delta_flags.
        self.assertEqual(
            order,
            [
                "noise_floor",
                "centering",
                "circularity",
                "outer_deadzone",
                "asymmetry",
                "bitness",
                "tremor",
                "linearity",
            ],
        )


# ---------------------------------------------------------------------------
# Notes snippet helper
# ---------------------------------------------------------------------------


class NotesSnippetTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(modules_screen._notes_snippet(""), "")

    def test_short_returns_unchanged(self) -> None:
        self.assertEqual(modules_screen._notes_snippet("short"), "short")

    def test_long_truncates_with_ellipsis(self) -> None:
        long = "abcdefghij" * 12  # 120 chars
        snippet = modules_screen._notes_snippet(long, length=20)
        self.assertEqual(len(snippet), 20)
        self.assertTrue(snippet.endswith("…"))

    def test_collapses_whitespace_to_single_spaces(self) -> None:
        out = modules_screen._notes_snippet("line1\nline2\t  line3")
        self.assertEqual(out, "line1 line2 line3")


# ---------------------------------------------------------------------------
# Has-archive helper
# ---------------------------------------------------------------------------


class HasArchiveForSideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_empty_returns_false(self) -> None:
        self.assertFalse(modules_screen._has_archive_for_side(self.service, SIDE_LEFT))
        self.assertFalse(modules_screen._has_archive_for_side(self.service, SIDE_RIGHT))

    def test_after_swap_returns_true_for_swapped_side(self) -> None:
        self.service.assign(SIDE_LEFT, "FIRST")
        self.service.assign(SIDE_LEFT, "SECOND")
        self.assertTrue(modules_screen._has_archive_for_side(self.service, SIDE_LEFT))
        # Right side untouched.
        self.assertFalse(modules_screen._has_archive_for_side(self.service, SIDE_RIGHT))


# ---------------------------------------------------------------------------
# Diagnostic-bundle export modal + generation
# ---------------------------------------------------------------------------


class ExportModalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = WearLedgerService(base_dir=self.root / "ledger")
        self.service = ModulePassportService(
            base_dir=self.root / "passports", wear_ledger=self.ledger
        )
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        self.bundle = DiagnosticBundleService(
            base_dir=self.root / "bundles",
            module_passport_service=self.service,
            wear_ledger=self.ledger,
            app_data_dir=self.root,
        )

    def test_list_view_renders_export_button_when_service_present(self) -> None:
        shell = _shell_with(self.service, diagnostic_bundle_service=self.bundle)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertIn("Export diagnostic bundle", labels)

    def test_list_view_omits_export_button_when_bundle_service_missing(self) -> None:
        shell = _shell_with(self.service, diagnostic_bundle_service=None)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = ps.button_labels()
        self.assertNotIn("Export diagnostic bundle", labels)

    def test_generate_markdown_writes_file_and_emits_event(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(
            self.service,
            state=state,
            diagnostic_bundle_service=self.bundle,
        )
        target = modules_screen._generate_markdown(shell, self.bundle, state)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertTrue(target.exists())
        self.assertTrue(target.name.endswith(".md"))
        events = self.ledger.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details.get("format"), "md")
        self.assertEqual(events[0].details.get("output_filename"), target.name)

    def test_generate_zip_writes_file_and_emits_event(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(
            self.service,
            state=state,
            diagnostic_bundle_service=self.bundle,
        )
        target = modules_screen._generate_zip(shell, self.bundle, state)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertTrue(target.exists())
        self.assertTrue(target.name.endswith(".zip"))
        events = self.ledger.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details.get("format"), "zip")

    def test_zip_export_opens_preview_before_writing(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(
            self.service,
            state=state,
            diagnostic_bundle_service=self.bundle,
        )

        def _run_swap(open_fn, **_kwargs):
            open_fn()

        shell._defer_modal_swap = MagicMock(side_effect=_run_swap)
        with patch(
            "zd_app.ui.screens.modules.diagnostic_bundle_preview.open_preview_modal"
        ) as open_preview:
            modules_screen._open_zip_preview(shell, self.bundle, state)

        shell._defer_modal_swap.assert_called_once()
        self.assertIn(
            modules_screen.TAG_EXPORT_MODAL,
            shell._defer_modal_swap.call_args.kwargs["delete_tags"],
        )
        open_preview.assert_called_once()
        events = self.ledger.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(events, [])

        open_preview.call_args.kwargs["on_cancel"]()
        events = self.ledger.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(events, [])

        with patch("zd_app.ui.screens.modules.os.startfile", create=True):
            open_preview.call_args.kwargs["on_export"]()
        events = self.ledger.read_events(
            event_types=[DIAGNOSTIC_BUNDLE_GENERATED]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details.get("format"), "zip")

    def test_export_state_defaults_are_sensible(self) -> None:
        state = modules_screen.ModulesScreenState()
        self.assertTrue(state.export_include_archived)
        self.assertEqual(state.export_health_limit, 5)
        self.assertEqual(state.export_wear_window_days, 90)


# ---------------------------------------------------------------------------
# i18n parity
# ---------------------------------------------------------------------------


class I18nParityTests(unittest.TestCase):
    """Every modules.* key in en.json must have a zh-CN counterpart."""

    EN_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "en.json"
    )
    ZH_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "zh-CN.json"
    )

    @classmethod
    def setUpClass(cls) -> None:
        import json

        cls.en_keys = set(json.loads(cls.EN_PATH.read_text(encoding="utf-8")).keys())
        cls.zh_keys = set(json.loads(cls.ZH_PATH.read_text(encoding="utf-8")).keys())

    def test_modules_keys_have_parity(self) -> None:
        en_modules = {k for k in self.en_keys if k.startswith("modules.")}
        zh_modules = {k for k in self.zh_keys if k.startswith("modules.")}
        missing_in_zh = en_modules - zh_modules
        missing_in_en = zh_modules - en_modules
        self.assertEqual(missing_in_zh, set(), f"zh-CN missing: {sorted(missing_in_zh)}")
        self.assertEqual(missing_in_en, set(), f"en missing: {sorted(missing_in_en)}")

    def test_nav_modules_present_in_both(self) -> None:
        self.assertIn("nav.modules", self.en_keys)
        self.assertIn("nav.modules", self.zh_keys)

    def test_module_event_keys_in_wear_ledger_section(self) -> None:
        for key in (
            "wear_ledger.event.module_assigned",
            "wear_ledger.event.module_characterized",
            "wear_ledger.event.module_notes_updated",
            "wear_ledger.filter.type.modules",
        ):
            self.assertIn(key, self.en_keys, f"en missing {key}")
            self.assertIn(key, self.zh_keys, f"zh-CN missing {key}")

    def test_diagnostic_bundle_event_key_present_in_both(self) -> None:
        for key in (
            "wear_ledger.event.diagnostic_bundle_generated",
            "wear_ledger.filter.type.bundles",
        ):
            self.assertIn(key, self.en_keys, f"en missing {key}")
            self.assertIn(key, self.zh_keys, f"zh-CN missing {key}")


# ---------------------------------------------------------------------------
# Forbidden-phrase scan for module-related copy
# ---------------------------------------------------------------------------


class ForbiddenPhraseScanTests(unittest.TestCase):
    """The module-passport family must follow the same honesty discipline
    as the Health Report — no implied latency / lag / health-score / etc.
    claims in user-facing copy.

    Word-boundary aware — "lag" must match the standalone word, not the
    "lag" substring inside "flagged".
    """

    FORBIDDEN_PHRASES = (
        "input latency",
        "input lag",
        "latency",
        "lag",
        "health score",
        "hardware health",
        "defect detected",
        "factory calibration",
        "lab-grade",
    )

    EN_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "en.json"
    )
    ZH_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "zh-CN.json"
    )

    def _scan(self, path: Path, prefix: str) -> None:
        import json
        import re

        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if not key.startswith(prefix):
                continue
            haystack = str(value).lower()
            for phrase in self.FORBIDDEN_PHRASES:
                pattern = r"\b" + re.escape(phrase) + r"\b"
                self.assertIsNone(
                    re.search(pattern, haystack),
                    f"Forbidden phrase {phrase!r} in {key} ({path.name})",
                )

    def test_en_modules_keys_no_forbidden_phrases(self) -> None:
        self._scan(self.EN_PATH, "modules.")

    def test_en_module_event_keys_no_forbidden_phrases(self) -> None:
        self._scan(self.EN_PATH, "wear_ledger.event.module_")

    def test_zh_modules_keys_no_forbidden_phrases(self) -> None:
        self._scan(self.ZH_PATH, "modules.")


# ---------------------------------------------------------------------------
# Trend analysis — LIST badge + TRENDS view + attention banner + hint
# ---------------------------------------------------------------------------


def _trend_fp(
    *,
    day_offset: float,
    base_ts: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    side: str = SIDE_LEFT,
    noise_floor_percent: float = 1.0,
    samples_count: int = 12_000,
) -> ModuleFingerprint:
    """Fingerprint at ``base_ts + day_offset`` days, otherwise standard."""

    ts = base_ts + timedelta(days=day_offset)
    return ModuleFingerprint(
        timestamp_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        side=side,
        duration_ms=60_000,
        samples_count=samples_count,
        noise_floor_percent=noise_floor_percent,
        centering_offset_x=0.2,
        centering_offset_y=-0.1,
        circularity_coverage_percent=0.97,
        outer_deadzone_min_axis=96.0,
        outer_deadzone_max_axis=99.0,
        asymmetry_score=0.04,
        bitness_observed=128,
        tremor_metric=0.18,
        linearity_score=0.05,
        overall_status=STATUS_GOOD,
        notes=None,
    )


class ListBadgeTests(unittest.TestCase):
    """The trend badge renders under each side card on the LIST view."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_insufficient_badge_for_single_fingerprint(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.append_fingerprint(SIDE_LEFT, _trend_fp(day_offset=0.0))
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # English label for insufficient_data.
        self.assertIn("Insufficient data", joined)

    def test_drifting_badge_after_eight_runs_with_slow_drift(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i),
            )
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Drifting", joined)
        # The "Trend:" badge text appears as a prefix; assert the row carries
        # a count of attention metrics (drifting/investigate metrics list
        # is appended to the badge for non-stable statuses).
        self.assertTrue(
            any("metric" in s and "Trend:" in s for s in ps.text_strings()),
            f"no 'Trend: ... metric(s)' badge found in {ps.text_strings()!r}",
        )

    def test_view_trends_button_present_when_passport_assigned(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK")
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertIn("View trend analysis", ps.button_labels())

    def test_no_badge_when_passport_unassigned(self) -> None:
        # An empty-side card shows the assign button, no trend badge.
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertNotIn("Trend:", joined)


class TrendsViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def _seed_drifting(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(day_offset=float(i), noise_floor_percent=1.0 + 0.01 * i),
            )

    def _seed_investigate(self) -> None:
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        for i in range(10):
            # noise_floor close to threshold → projection lands inside
            # INVESTIGATE_DAYS_TO_THRESHOLD (30 days) for "investigate".
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(
                    day_offset=float(i),
                    noise_floor_percent=5.5 + 0.05 * i,
                ),
            )

    def test_trends_view_renders_all_eight_metric_rows(self) -> None:
        self._seed_drifting()
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # All 8 metric names appear (they reuse the compare metric_name keys).
        for name in (
            "Noise floor",
            "Center offset",
            "Circularity coverage",
            "Outer reach span",
            "Asymmetry score",
            "Discrete-step count",
            "Tremor metric",
            "Linearity score",
        ):
            self.assertIn(name, joined, f"missing metric label {name!r}")

    def test_trends_view_back_button_returns_to_detail(self) -> None:
        self._seed_drifting()
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertIn("Back to detail", ps.button_labels())

    def test_trends_view_renders_overall_status(self) -> None:
        self._seed_drifting()
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Overall trend", joined)
        self.assertIn("Drifting", joined)

    def test_trends_view_renders_attention_banner_when_investigate(self) -> None:
        self._seed_investigate()
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # English banner title contains "Attention".
        self.assertIn("Attention", joined)
        # And lists the driving metric (noise floor).
        self.assertIn("Noise floor", joined)

    def test_trends_view_no_banner_when_drifting(self) -> None:
        # Drifting status should NOT show the attention banner — the banner
        # is reserved for "investigate", which is the more urgent state.
        self._seed_drifting()
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # The "Attention — trend movement" banner only fires for investigate.
        self.assertNotIn("Attention — trend movement", joined)

    def test_insufficient_data_view_renders_cleanly_with_one_fingerprint(
        self,
    ) -> None:
        # Backward-compat: N=1 must not crash, must render the trends view
        # with "Insufficient data" labelling.
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        self.service.append_fingerprint(SIDE_LEFT, _trend_fp(day_offset=0.0))
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Insufficient data", joined)
        # No crashes — confirmed by reaching this assertion.

    def test_no_passport_renders_empty_card_body(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("No module assigned", joined)


class TrendProjectionTests(unittest.TestCase):
    """The TRENDS view's per-metric row shows "projected days" ONLY for
    moderate-confidence degrading metrics with positive runway.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_projection_row_appears_for_degrading_metric(self) -> None:
        # 10 fingerprints with steep upward slope close to threshold.
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(10):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(
                    day_offset=float(i), noise_floor_percent=5.5 + 0.05 * i
                ),
            )
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Projected to reach", joined)

    def test_no_projection_row_for_stable_metric(self) -> None:
        # Flat noise_floor over 8 days → stable, no projection.
        self.service.assign(SIDE_LEFT, "STOCK")
        for i in range(8):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(day_offset=float(i), noise_floor_percent=1.0),
            )
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # The "No actionable projection" copy is what renders for every
        # stable metric. The "Projected to reach" copy must not appear at
        # all when there's no degrading metric.
        self.assertIn("No actionable projection", joined)
        self.assertNotIn("Projected to reach", joined)


class RecharacterizeHintTests(unittest.TestCase):
    """The "your last run was a while ago" hint fires past ATTENTION_AGE_DAYS."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))

    def test_hint_renders_when_last_fingerprint_is_stale(self) -> None:
        # Single fingerprint from far in the past — service.utc_now is the
        # real clock so the fingerprint is days/months old.
        self.service.assign(SIDE_LEFT, "STOCK")
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(days=120)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_fp = ModuleFingerprint(
            timestamp_utc=stale_ts,
            side=SIDE_LEFT,
            duration_ms=60_000,
            samples_count=12_000,
            noise_floor_percent=1.0,
            centering_offset_x=0.0,
            centering_offset_y=0.0,
            circularity_coverage_percent=0.97,
            outer_deadzone_min_axis=96.0,
            outer_deadzone_max_axis=99.0,
            asymmetry_score=0.04,
            bitness_observed=128,
            tremor_metric=0.18,
            linearity_score=0.05,
            overall_status=STATUS_GOOD,
            notes=None,
        )
        self.service.append_fingerprint(SIDE_LEFT, stale_fp)
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # English copy: "Last characterization was over {days} days ago."
        self.assertIn("Last characterization was over", joined)

    def test_no_hint_for_recent_fingerprint(self) -> None:
        # Today's fingerprint — hint must not render.
        self.service.assign(SIDE_LEFT, "STOCK")
        self.service.append_fingerprint(
            SIDE_LEFT,
            _trend_fp(day_offset=0.0, base_ts=datetime.now(timezone.utc)),
        )
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_MODULE_TRENDS,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertNotIn("Last characterization was over", joined)


class DetailViewAttentionBannerTests(unittest.TestCase):
    """The attention banner renders at the TOP of the detail view too."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ModulePassportService(base_dir=Path(self._tmp.name))
        self.service.assign(SIDE_LEFT, "STOCK_LEFT")
        for i in range(10):
            self.service.append_fingerprint(
                SIDE_LEFT,
                _trend_fp(
                    day_offset=float(i), noise_floor_percent=5.5 + 0.05 * i
                ),
            )

    def test_detail_view_renders_attention_banner_for_investigate(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("Attention", joined)

    def test_detail_view_has_view_trends_button(self) -> None:
        state = modules_screen.ModulesScreenState(
            view=modules_screen.VIEW_DETAIL,
            detail_side=SIDE_LEFT,
        )
        shell = _shell_with(self.service, state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertIn("View trend analysis", ps.button_labels())


class NavigationToTrendsTests(unittest.TestCase):
    def test_open_module_trends_sets_state(self) -> None:
        state = modules_screen.ModulesScreenState()
        shell = _shell_with(None, state=state)
        modules_screen._on_open_module_trends(shell, SIDE_RIGHT)
        self.assertEqual(state.view, modules_screen.VIEW_MODULE_TRENDS)
        self.assertEqual(state.detail_side, SIDE_RIGHT)


class TrendsI18nKeysTests(unittest.TestCase):
    """The new modules.trends.* keys must exist in both locales + the new
    wear_ledger.event.module_trend_flagged key too."""

    EN_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "en.json"
    )
    ZH_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "zh-CN.json"
    )

    REQUIRED_KEYS = (
        "modules.trends.title",
        "modules.trends.subtitle",
        "modules.trends.back_button",
        "modules.trends.overall_label",
        "modules.trends.per_metric_heading",
        "modules.trends.status.stable",
        "modules.trends.status.drifting",
        "modules.trends.status.investigate",
        "modules.trends.status.insufficient_data",
        "modules.trends.confidence.high",
        "modules.trends.confidence.moderate",
        "modules.trends.confidence.low",
        "modules.trends.confidence.insufficient_data",
        "modules.trends.attention_banner.title",
        "modules.trends.attention_banner.body",
        "modules.trends.recharacterize_hint",
        "modules.trends.metric_row.value_and_slope",
        "modules.trends.metric_row.confidence_status",
        "modules.trends.metric_row.projection",
        "modules.trends.metric_row.no_projection",
        "modules.list.trend_badge",
        "modules.list.trend_badge_with_count",
        "modules.list.button.view_trends",
        "modules.detail.view_trends_button",
        "wear_ledger.event.module_trend_flagged",
    )

    @classmethod
    def setUpClass(cls) -> None:
        import json

        cls.en_keys = set(json.loads(cls.EN_PATH.read_text(encoding="utf-8")).keys())
        cls.zh_keys = set(json.loads(cls.ZH_PATH.read_text(encoding="utf-8")).keys())

    def test_required_keys_present_in_en(self) -> None:
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, self.en_keys, f"en missing {key}")

    def test_required_keys_present_in_zh(self) -> None:
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, self.zh_keys, f"zh-CN missing {key}")


class TrendsForbiddenPhraseTests(unittest.TestCase):
    """No "wear claim" / "health score" language in trends copy."""

    EN_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "en.json"
    )
    ZH_PATH = (
        Path(__file__).resolve().parent.parent
        / "zd_app" / "i18n" / "locales" / "zh-CN.json"
    )

    FORBIDDEN_PHRASES = (
        "input latency",
        "input lag",
        "latency",
        "lag",
        "health score",
        "hardware health",
        "defect detected",
        "factory calibration",
        "lab-grade",
        "broken",
        "failing",
    )

    def _scan(self, path: Path, prefix: str) -> None:
        import json
        import re

        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if not key.startswith(prefix):
                continue
            haystack = str(value).lower()
            for phrase in self.FORBIDDEN_PHRASES:
                pattern = r"\b" + re.escape(phrase) + r"\b"
                self.assertIsNone(
                    re.search(pattern, haystack),
                    f"Forbidden phrase {phrase!r} in {key} ({path.name})",
                )

    def test_en_trends_keys_no_forbidden_phrases(self) -> None:
        self._scan(self.EN_PATH, "modules.trends.")

    def test_en_trends_event_no_forbidden_phrases(self) -> None:
        self._scan(self.EN_PATH, "wear_ledger.event.module_trend_flagged")

    def test_zh_trends_keys_no_forbidden_phrases(self) -> None:
        self._scan(self.ZH_PATH, "modules.trends.")


if __name__ == "__main__":
    unittest.main()
