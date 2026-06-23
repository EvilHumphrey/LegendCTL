"""DPG screen-build tests for the Wear Ledger screen.

Patches the ``dpg`` symbols on the screen module so ``build()`` runs without
a real DPG context. Each test verifies the right widgets render for a given
service state and that filter / modal callbacks update the screen state and
invoke a rebuild.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional
from unittest.mock import MagicMock, patch

import dearpygui.dearpygui as dpg

from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    HEALTH_REPORT,
    MODULE_ASSIGNED,
    MODULE_CHARACTERIZED,
    PROFILE_APPLY,
    RP_CAPTURE,
    SERVICE_NOTE,
    SESSION_START,
    SLIDER_WRITE,
)
from zd_app.ui.screens import wear_ledger as screen


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


def _frozen_clock(start: datetime) -> Callable[[], datetime]:
    state = {"now": start}

    def _now() -> datetime:
        current = state["now"]
        state["now"] = current + timedelta(seconds=1)
        return current

    return _now


def _shell_with(
    service: Optional[WearLedgerService],
    *,
    screen_state: Optional[screen.WearLedgerScreenState] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        wear_ledger_service=service,
        wear_ledger_screen_state=screen_state,
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


def _make_service(tmp_path: Path, clock: Callable[[], datetime]) -> WearLedgerService:
    return WearLedgerService(base_dir=tmp_path, utc_now=clock)


class _PatchedScreen:
    """Context manager that captures DPG calls + button callbacks."""

    def __init__(self, shell) -> None:
        self.shell = shell
        self.values: dict[str, object] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.buttons: list[dict] = []
        self.combos: list[dict] = []
        self.child_windows: list[dict] = []
        self.windows: list[dict] = []
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
                if name == "add_input_text":
                    self.values[kw.get("tag", "")] = ""
                return kw.get("tag", name)
            return _fn

        def record_child_window(*args, **kw):
            self.calls.append(("child_window", args, kw))
            self.child_windows.append(kw)
            return _FakeContextManager()

        def record_window(*args, **kw):
            self.calls.append(("window", args, kw))
            self.windows.append(kw)
            return _FakeContextManager()

        # Table primitives. ``table`` / ``table_row`` are context managers in
        # the screen (``with dpg.table(...)``); ``add_table_column`` is a plain
        # call. They are NOT in the dpg recorder by default, and patching the
        # screen module's ``dpg`` mutates the shared dearpygui module object —
        # so without these stubs the real (context-less) table calls segfault.
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

        def set_value(tag, value):
            self.values[tag] = value

        def get_value(tag):
            return self.values.get(tag, "")

        self._cm = patch.multiple(
            "zd_app.ui.screens.wear_ledger.dpg",
            add_text=record("add_text"),
            add_spacer=record("add_spacer"),
            add_button=record("add_button"),
            add_combo=record("add_combo"),
            add_input_text=record("add_input_text"),
            child_window=record_child_window,
            window=record_window,
            table=record_table,
            table_row=record_table_row,
            add_table_column=record_table_column,
            group=_FakeGroup,
            set_value=set_value,
            get_value=get_value,
            does_item_exist=lambda *_a, **_kw: False,
            delete_item=lambda *_a, **_kw: None,
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
        out: list[str] = []
        for fn, args, kw in self.calls:
            if fn == "add_text":
                if args:
                    out.append(str(args[0]))
                elif "default_value" in kw:
                    out.append(str(kw["default_value"]))
        return out

    def combo_tags(self) -> list[str]:
        return [kw.get("tag") for kw in self.combos]

    def column_labels(self) -> list[str]:
        return [kw.get("label") for kw in self.columns]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class UnavailableServiceTests(unittest.TestCase):
    def test_renders_unavailable_text_when_service_is_none(self) -> None:
        shell = _shell_with(None)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The unavailable message should appear in some add_text call.
        joined = "\n".join(ps.text_strings())
        self.assertIn("unavailable", joined.lower())
        # No filter combos or list rows should render.
        self.assertEqual(ps.combo_tags(), [])


class EmptyStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )

    def test_renders_empty_state_copy_when_no_events(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # Empty state copy mentions "no events" or "appear here"
        self.assertTrue(
            "no events" in joined.lower() or "appear here" in joined.lower(),
            joined,
        )

    def test_filter_combos_render_even_when_empty(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertIn(screen.TAG_TYPE_FILTER_COMBO, ps.combo_tags())
        self.assertIn(screen.TAG_RANGE_FILTER_COMBO, ps.combo_tags())
        self.assertIn(screen.TAG_ADD_NOTE_BUTTON, ps.button_tags())


class ListRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )
        self.service.append(SESSION_START, summary="session A")
        self.service.append(PROFILE_APPLY, summary="profile race-pad applied")
        # Only this event carries details, so only its row gets an expand
        # toggle (the bare session / profile rows have nothing to expand).
        self.service.append(
            RP_CAPTURE,
            summary="captured manual RP",
            details={"label": "manual", "fields_written": 12},
        )

    def test_renders_one_table_row_per_event(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The event log is now a single native table: exactly one table_row
        # per event (no more one-bordered-child-window-per-event stack).
        self.assertEqual(len(ps.table_rows), 3)
        self.assertEqual(len(ps.tables), 1)

    def test_event_log_table_has_time_type_status_detail_columns(self) -> None:
        from zd_app.i18n import t

        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertEqual(
            ps.column_labels(),
            [
                t("wear_ledger.table.col_time"),
                t("wear_ledger.table.col_type"),
                t("wear_ledger.table.col_status"),
                t("wear_ledger.table.col_detail"),
            ],
        )

    def test_only_events_with_details_get_an_expand_button(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # Only the RP_CAPTURE row has details → exactly one "Show details".
        expand_count = sum(
            1
            for kw in ps.buttons
            if kw.get("label", "").lower().startswith("show details")
        )
        self.assertEqual(expand_count, 1)

    def test_expanded_row_shows_detail_pairs(self) -> None:
        state = screen.WearLedgerScreenState()
        # Pin the expansion to the newest event (RP_CAPTURE, which has details).
        all_events = self.service.read_events()
        state.expanded_event_ts = all_events[0].ts
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        labels = [kw.get("label", "") for kw in ps.buttons]
        self.assertIn("Hide details", labels)
        # The detail key/value pairs render as muted text under the summary.
        joined = "\n".join(ps.text_strings())
        self.assertIn("fields_written", joined)
        self.assertIn("manual", joined)


class FilterCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )
        self.service.append(SESSION_START, summary="s1")
        self.service.append(PROFILE_APPLY, summary="p1")

    def test_type_filter_changes_state_and_triggers_rebuild(self) -> None:
        from zd_app.i18n import t

        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        # The combo's callback is what we want to exercise.
        screen._on_type_filter_changed(shell, t("wear_ledger.filter.type.profiles"))
        self.assertEqual(state.type_filter, "profiles")
        shell.rebuild_current_screen.assert_called()

    def test_range_filter_changes_state_and_triggers_rebuild(self) -> None:
        from zd_app.i18n import t

        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        screen._on_range_filter_changed(shell, t("wear_ledger.filter.range.30"))
        self.assertEqual(state.range_filter, "30")
        shell.rebuild_current_screen.assert_called()

    def test_unknown_filter_label_is_ignored(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        screen._on_type_filter_changed(shell, "Garbage Label")
        self.assertEqual(state.type_filter, "all")  # unchanged
        shell.rebuild_current_screen.assert_not_called()

    def test_type_filter_actually_narrows_visible_rows(self) -> None:
        state = screen.WearLedgerScreenState()
        state.type_filter = "profiles"
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # Only the PROFILE_APPLY event passes the filter → one table row.
        self.assertEqual(len(ps.table_rows), 1)

    def test_range_filter_excludes_old_events(self) -> None:
        # Self-contained service: the screen's range filter reads the REAL
        # ``datetime.now`` (see ``_range_filter_to_since`` in the screen),
        # so the "recent" fixtures must be stamped relative to real now —
        # NOT via the frozen setUp clock, whose fixed date would drift out
        # of the window as wall-clock time advances (this test used to rot
        # ~7 days after the setUp date).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = _make_service(
            Path(tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        service.append(SESSION_START, summary="s1", ts=recent)
        service.append(PROFILE_APPLY, summary="p1", ts=recent)
        past = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        service.append(SLIDER_WRITE, summary="very old", ts=past)
        state = screen.WearLedgerScreenState()
        state.range_filter = "7"
        shell = _shell_with(service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # The two recent appends pass; the 2025 SLIDER_WRITE is trimmed.
        # Robust as wall-clock advances (``recent`` is always <7d old).
        self.assertEqual(len(ps.table_rows), 2)


class ServiceNoteSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )

    def test_save_service_note_persists_and_returns_event(self) -> None:
        shell = _shell_with(self.service)
        result = screen._save_service_note(shell, "Installed K-Silver modules\n")
        self.assertIsNotNone(result)
        events = self.service.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, SERVICE_NOTE)
        self.assertIn("K-Silver", events[0].details["note"])

    def test_save_service_note_empty_input_is_rejected_with_warning(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        result = screen._save_service_note(shell, "   \n\t  ")
        self.assertIsNone(result)
        self.assertEqual(state.status_kind, "warn")
        self.assertEqual(self.service.count_events(), 0)

    def test_save_service_note_when_service_is_none_warns(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(None, screen_state=state)
        result = screen._save_service_note(shell, "anything")
        self.assertIsNone(result)
        self.assertEqual(state.status_kind, "warn")

    def test_save_service_note_strips_control_chars(self) -> None:
        shell = _shell_with(self.service)
        screen._save_service_note(shell, "before\x00after")
        events = self.service.read_events()
        self.assertEqual(events[0].details["note"], "beforeafter")


class SparklineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )

    def test_sparkline_empty_message_renders_with_no_health_events(self) -> None:
        self.service.append(SESSION_START, summary="hi")
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        self.assertIn("verdict trend", joined.lower())

    def test_sparkline_renders_filled_square_per_verdict(self) -> None:
        # Three health reports with distinct verdicts.
        for verdict in ("normal", "tuning_suggested", "possible_issue"):
            self.service.append(
                HEALTH_REPORT,
                summary=f"hr {verdict}",
                details={"overall_status": verdict, "caveats_count": 0},
            )
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        squares = [s for s in ps.text_strings() if s == "■"]
        self.assertEqual(len(squares), 3)

    def test_sparkline_legend_includes_count(self) -> None:
        for verdict in ("normal", "normal"):
            self.service.append(
                HEALTH_REPORT,
                summary="hr",
                details={"overall_status": verdict},
            )
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        joined = "\n".join(ps.text_strings())
        # "2 of last 20 verdicts shown" — substring "20" from the constant.
        self.assertIn("20", joined)

    def test_verdict_trend_card_is_height_pinned_compact(self) -> None:
        # Regression: the verdict-trend card must reserve a compact, fixed
        # block so the event-log table stays near the top. A 2026 smoke showed
        # an autosize card reserving a tall block that pushed the table ~470px
        # down past dead space. The card is the only height-sized child_window
        # on the screen (root container + log card both autosize_y).
        for verdict in ("normal", "tuning_suggested"):
            self.service.append(
                HEALTH_REPORT, summary="hr", details={"overall_status": verdict}
            )
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        sized = [kw["height"] for kw in ps.child_windows if kw.get("height") is not None]
        self.assertEqual(sized, [screen._SPARKLINE_CARD_HEIGHT])
        # Compact enough that the event log is not pushed below the fold.
        self.assertLessEqual(screen._SPARKLINE_CARD_HEIGHT, 200)


class ToggleExpandTests(unittest.TestCase):
    def test_toggle_expand_sets_state(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(None, screen_state=state)
        screen._on_toggle_expand(shell, "2026-05-26T12:00:00Z")
        self.assertEqual(state.expanded_event_ts, "2026-05-26T12:00:00Z")
        # Toggling again collapses.
        screen._on_toggle_expand(shell, "2026-05-26T12:00:00Z")
        self.assertIsNone(state.expanded_event_ts)


class ModuleFilterTests(unittest.TestCase):
    """Third filter combo: "Module: All / <module_ids>" populated from events."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )
        # Three module-tagged events plus one untagged session.
        self.service.append(
            MODULE_ASSIGNED,
            summary="assigned alpha",
            details={"side": "left", "module_id": "ALPHA"},
        )
        self.service.append(
            MODULE_CHARACTERIZED,
            summary="characterized alpha",
            details={"side": "left", "module_id": "ALPHA", "overall_status": "good"},
        )
        self.service.append(
            MODULE_ASSIGNED,
            summary="assigned beta",
            details={"side": "right", "module_id": "BETA"},
        )
        self.service.append(SESSION_START, summary="session unrelated")

    def test_module_combo_renders_with_options(self) -> None:
        shell = _shell_with(self.service)
        with _PatchedScreen(shell) as ps:
            ps.build()
        combo_tags = ps.combo_tags()
        self.assertIn(screen.TAG_MODULE_FILTER_COMBO, combo_tags)
        # Find the module combo's items list.
        module_combo = next(
            kw for kw in ps.combos if kw.get("tag") == screen.TAG_MODULE_FILTER_COMBO
        )
        items = module_combo.get("items", [])
        # Both unique module_ids surface, plus the "All modules" sentinel.
        self.assertIn("ALPHA", items)
        self.assertIn("BETA", items)
        # First item is the "All modules" translated label.
        from zd_app.i18n import t

        self.assertEqual(items[0], t("wear_ledger.filter.module.all"))

    def test_module_filter_narrows_visible_rows(self) -> None:
        state = screen.WearLedgerScreenState()
        state.module_filter = "ALPHA"
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # Only the two ALPHA-tagged events pass; BETA and the session do not.
        self.assertEqual(len(ps.table_rows), 2)

    def test_module_filter_all_shows_everything(self) -> None:
        state = screen.WearLedgerScreenState()
        state.module_filter = screen.MODULE_FILTER_ALL
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # All four events render: ALPHA×2 + BETA + session.
        self.assertEqual(len(ps.table_rows), 4)

    def test_module_filter_callback_updates_state(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        # Module options pulled from the event log; calling with the matching
        # label sets the filter.
        options = screen._module_filter_options(self.service.read_events())
        screen._on_module_filter_changed(shell, "BETA", options)
        self.assertEqual(state.module_filter, "BETA")
        shell.rebuild_current_screen.assert_called()

    def test_module_filter_self_heals_to_all_when_id_disappears(self) -> None:
        # Pin module_filter to a value that doesn't exist in the event log.
        state = screen.WearLedgerScreenState()
        state.module_filter = "GHOST_MODULE_THAT_NEVER_WAS"
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        # build() resets module_filter back to "all" so the operator sees
        # something instead of an empty list.
        self.assertEqual(state.module_filter, screen.MODULE_FILTER_ALL)

    def test_module_filter_unknown_label_is_ignored(self) -> None:
        state = screen.WearLedgerScreenState()
        shell = _shell_with(self.service, screen_state=state)
        screen._on_module_filter_changed(shell, "Garbage Label", (screen.MODULE_FILTER_ALL,))
        self.assertEqual(state.module_filter, screen.MODULE_FILTER_ALL)
        shell.rebuild_current_screen.assert_not_called()

    def test_module_filter_combines_with_type_filter(self) -> None:
        # Combine module=ALPHA with type=modules — only the two ALPHA events
        # should pass (both happen to be module events anyway, but if a
        # session event had ALPHA module_id it would still be excluded).
        state = screen.WearLedgerScreenState()
        state.module_filter = "ALPHA"
        state.type_filter = "modules"
        shell = _shell_with(self.service, screen_state=state)
        with _PatchedScreen(shell) as ps:
            ps.build()
        self.assertEqual(len(ps.table_rows), 2)

    def test_module_options_helper_dedupes_and_sorts(self) -> None:
        # Reach the helper directly so the sorting + dedupe contract is
        # locked in independently of the service.
        events = self.service.read_events()
        options = screen._module_filter_options(events)
        self.assertEqual(options[0], screen.MODULE_FILTER_ALL)
        self.assertEqual(sorted(options[1:]), list(options[1:]))
        # Each unique id appears exactly once.
        ids = options[1:]
        self.assertEqual(sorted(ids), ["ALPHA", "BETA"])


class WearLedgerModuleFilterI18nTests(unittest.TestCase):
    def test_module_filter_keys_present_in_both_locales(self) -> None:
        import json
        from zd_app.i18n import _locale_dir

        locale_dir = _locale_dir()
        with open(locale_dir / "en.json", encoding="utf-8") as f:
            en = json.load(f)
        with open(locale_dir / "zh-CN.json", encoding="utf-8") as f:
            zh = json.load(f)
        for key in (
            "wear_ledger.filter.module_label",
            "wear_ledger.filter.module.all",
        ):
            self.assertIn(key, en, f"en missing {key}")
            self.assertIn(key, zh, f"zh-CN missing {key}")


class I18nParityTests(unittest.TestCase):
    """Sanity-check that en + zh-CN have the same wear_ledger.* key set."""

    def setUp(self) -> None:
        import json
        from zd_app.i18n import _locale_dir

        locale_dir = _locale_dir()
        with open(locale_dir / "en.json", encoding="utf-8") as f:
            self.en = json.load(f)
        with open(locale_dir / "zh-CN.json", encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_wear_ledger_keys_have_locale_parity(self) -> None:
        en_keys = {k for k in self.en if k.startswith("wear_ledger.")}
        zh_keys = {k for k in self.zh if k.startswith("wear_ledger.")}
        self.assertEqual(en_keys, zh_keys)
        self.assertGreater(len(en_keys), 30)

    def test_nav_wear_ledger_exists_in_both(self) -> None:
        self.assertIn("nav.wear_ledger", self.en)
        self.assertIn("nav.wear_ledger", self.zh)

    def test_wear_ledger_keys_no_forbidden_phrases(self) -> None:
        forbidden = ("factory_", "image_", "clone_", "backup_image")
        for key in self.en:
            if not key.startswith("wear_ledger."):
                continue
            value = self.en[key].lower()
            for token in forbidden:
                self.assertNotIn(
                    token,
                    value,
                    f"wear_ledger key {key} contains forbidden token {token!r}",
                )

    def test_each_event_type_has_a_label_key(self) -> None:
        from zd_app.services.wear_ledger.models import EVENT_TYPES

        for event_type in EVENT_TYPES:
            key = f"wear_ledger.event.{event_type}"
            self.assertIn(key, self.en, f"missing en key {key}")
            self.assertIn(key, self.zh, f"missing zh-CN key {key}")


class RealContextTableRenderTests(unittest.TestCase):
    """Build the screen against a REAL DPG context (not the recorder).

    The recorder stubs ``table`` / ``table_row`` / ``card`` and never touches
    Dear PyGui, so it cannot catch a malformed real table (bad column policy,
    an unsupported button flag, a card/autosize clash, …). This class drives
    the actual widgets the way ``test_screens_home`` does for the Phase-2 Home
    pilot, then inspects the live item tree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(
            Path(self._tmp.name),
            _frozen_clock(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)),
        )

    def _build_in_context(self) -> None:
        dpg.create_context()
        with dpg.window():
            with dpg.child_window(tag="content_region"):
                pass
        screen.build(_shell_with(self.service), parent="content_region")

    def _text_values(self) -> list[str]:
        out: list[str] = []
        for item in dpg.get_all_items():
            if dpg.get_item_type(item) == "mvAppItemType::mvText":
                value = dpg.get_value(item)
                if value is not None:
                    out.append(value)
        return out

    def test_event_log_renders_a_real_table_widget(self) -> None:
        self.service.append(SESSION_START, summary="session A")
        self.service.append(
            HEALTH_REPORT,
            summary="health ok",
            details={"overall_status": "normal", "caveats_count": 0},
        )
        try:
            self._build_in_context()
            self.assertTrue(dpg.does_item_exist(screen.TAG_EVENT_TABLE))
            self.assertEqual(
                dpg.get_item_type(screen.TAG_EVENT_TABLE),
                "mvAppItemType::mvTable",
            )
        finally:
            dpg.destroy_context()

    def test_verdict_event_renders_status_dot_and_sparkline_square(self) -> None:
        self.service.append(
            HEALTH_REPORT,
            summary="health ok",
            details={"overall_status": "normal"},
        )
        try:
            self._build_in_context()
            values = self._text_values()
            # ● = per-row status chip; ■ = sparkline trend square. Both render
            # for a health-report event, with deliberately distinct glyphs.
            self.assertIn("●", values)
            self.assertIn("■", values)
        finally:
            dpg.destroy_context()

    def test_empty_log_renders_no_table(self) -> None:
        # No events → empty-state copy, and crucially no table widget at all.
        try:
            self._build_in_context()
            self.assertFalse(dpg.does_item_exist(screen.TAG_EVENT_TABLE))
        finally:
            dpg.destroy_context()


if __name__ == "__main__":  # pragma: no cover - manual driver
    unittest.main()
