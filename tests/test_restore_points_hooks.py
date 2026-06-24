"""Apply-pipeline hook tests for Restore Points (RPU4 + RPU5).

Each test stubs ``shell.restore_point_service`` with a :class:`_RecordingService`
and exercises the shell entry point that should fire ``service.capture(...)``
with the expected trigger. The fakes are intentionally permissive so the
tests can assert call counts + trigger types without depending on the real
SettingsService / SettingsApplyCoordinator implementations.

Coverage:

- ``_apply_wrapper_profile_resolved`` fires
  ``before_profile_apply_with_device_settings`` when ``include_device=True``
  and NOT when ``include_device=False``.
- ``_create_safe_import_restore_point`` fires ``before_safe_import_apply``
  via the service and migrates off the old WrapperProfileStore bridge.
- ``apply_step_size`` + ``apply_polling_rate`` fire
  ``before_manual_device_setting_write`` and are debounced — a second call
  within ``MANUAL_DEVICE_WRITE_RP_WINDOW_S`` is skipped.
- ``refresh_from_controller`` fires ``first_readable_connect`` exactly once
  per identity per session.
- ``manual_save_restore_point`` fires ``manual`` and propagates the title.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.r2_shell_test_helpers import make_shell
from zd_app.services.settings_apply_coordinator import outcome_label
from zd_app.services.settings_service import (
    ControllerSnapshot,
    PollingRate,
)
from zd_app.storage.restore_point_models import (
    RestorePointTrigger,
)
from zd_app.ui import app_shell as app_shell_module


class _RecordingService:
    """Records every :meth:`capture` call without doing any IO."""

    def __init__(self) -> None:
        self.captures: list[tuple[str, str | None, str | None]] = []
        self._counter = 0
        # Screen path may also call list_with_skipped via the shell at some
        # point — keep it accessible even though hook tests don't use it.
        self.list_with_skipped = MagicMock(return_value=([], []))

    def capture(
        self,
        trigger: RestorePointTrigger,
        *,
        title: str | None = None,
        device_identity=None,
        fresh_read_max_age_s: float = 30.0,
        cached_snapshot=None,
        cached_snapshot_ts=None,
    ):
        self._counter += 1
        rp_id = f"rp_recorded_{self._counter}"
        self.captures.append((trigger.type, title, rp_id))
        return SimpleNamespace(
            id=rp_id,
            title=title or f"{trigger.source_label} — recorded",
            trigger=trigger,
        )

    def restore(self, *args, **kwargs):
        raise AssertionError("restore() unexpected in hook tests")


def _make_shell_with_recording_rp_service():
    """Build a shell whose ``restore_point_service`` is a recording stub."""

    settings_service = MagicMock()
    # Polling-rate combo callbacks check hydration state; mark it hydrated
    # so the apply_polling_rate / apply_step_size live-write callbacks
    # actually proceed past the read-miss guard.
    settings_service.set_polling_rate.return_value = SimpleNamespace(
        outcome="success",
    )
    settings_service.set_step_size.return_value = SimpleNamespace(
        outcome="success",
    )
    # The live slider path writes via the plain set_step_size; the apply path
    # uses the verified setter. Delegate verified -> set_step_size so either path
    # records through the same return value the RP-hook tests expect.
    settings_service.set_step_size_verified.side_effect = (
        lambda value, *a, **k: settings_service.set_step_size(value)
    )
    rp_service = _RecordingService()
    shell = make_shell(settings_service=settings_service, restore_point_service=rp_service)
    shell._polling_rate_hydrated = True
    shell._step_size_hydrated = True
    return shell, rp_service


def _trigger_types(service: _RecordingService) -> list[str]:
    return [trigger for trigger, _, _ in service.captures]


# ---------------------------------------------------------------------------
# before_profile_apply_with_device_settings
# ---------------------------------------------------------------------------


class ProfileApplyHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()
        # Stub the snapshot apply so the resolved-apply call doesn't touch
        # the real coordinator; we only care that the hook fires.
        self.shell._apply_wrapper_profile_snapshot = MagicMock()

    def _profile(self, *, with_device: bool):
        snapshot = ControllerSnapshot(
            polling_rate=PollingRate.HZ_1000 if with_device else None,
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
            motion_settings=None,
            back_paddle_bindings={},
            step_size=128 if with_device else None,
        )
        return SimpleNamespace(name="Apex", snapshot=snapshot)

    def test_include_device_true_fires_before_profile_apply_with_device_settings(self) -> None:
        profile = self._profile(with_device=True)
        self.shell._apply_wrapper_profile_resolved(
            "Apex", profile, include_device=True
        )
        self.assertIn(
            "before_profile_apply_with_device_settings",
            _trigger_types(self.service),
        )

    def test_include_device_false_does_not_fire_capture(self) -> None:
        profile = self._profile(with_device=False)
        self.shell._apply_wrapper_profile_resolved(
            "Apex", profile, include_device=False
        )
        self.assertEqual(self.service.captures, [])


# ---------------------------------------------------------------------------
# before_safe_import_apply
# ---------------------------------------------------------------------------


class SafeImportHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_safe_import_path_fires_before_safe_import_apply(self) -> None:
        result = self.shell._create_safe_import_restore_point()
        self.assertEqual(_trigger_types(self.service), ["before_safe_import_apply"])
        self.assertIsNotNone(result)

    def test_safe_import_path_returns_none_when_service_missing(self) -> None:
        self.shell.restore_point_service = None
        result = self.shell._create_safe_import_restore_point()
        self.assertIsNone(result)

    def test_safe_import_path_no_longer_writes_to_wrapper_profile_store(self) -> None:
        # The old bridge persisted via wrapper_profile_store.save(); the new
        # path goes through RestorePointService.capture(). Verify the bridge
        # is gone — the wrapper-profile store must not receive a save call
        # from this path even when a restore point is created.
        self.shell.wrapper_profile_store.save.reset_mock()
        self.shell._create_safe_import_restore_point()
        self.shell.wrapper_profile_store.save.assert_not_called()


# ---------------------------------------------------------------------------
# before_manual_device_setting_write (debounce)
# ---------------------------------------------------------------------------


class ManualDeviceWriteHookTests(unittest.TestCase):
    """Drive the hook helper directly.

    Calling :meth:`AppShell.apply_step_size` / :meth:`apply_polling_rate`
    chains into :meth:`_record_settings_apply_result` +
    :meth:`refresh_shell`, both of which touch DPG / the title manager /
    ctypes layers that segfault at process exit when invoked without a
    real DPG context. The hook itself is what we want to test — the
    apply-method wrappers are thin orchestration around it.
    """

    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_step_size_hook_fires_once_then_debounces(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["before_manual_device_setting_write"])

    def test_polling_rate_hook_fires_once_then_debounces(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["before_manual_device_setting_write"])

    def test_separate_fields_dont_share_debounce_state(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        self.shell._maybe_capture_before_manual_device_write(field_key="polling_rate")
        # Both should fire because the debounce is per-field-key.
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, [
            "before_manual_device_setting_write",
            "before_manual_device_setting_write",
        ])

    def test_capture_fires_again_after_debounce_window_elapses(self) -> None:
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        # Rewind the debounce timestamp so the next call is past the window.
        self.shell._last_manual_rp_for_field["step_size"] -= (
            app_shell_module.MANUAL_DEVICE_WRITE_RP_WINDOW_S + 1.0
        )
        self.shell._maybe_capture_before_manual_device_write(field_key="step_size")
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(len(captures), 2)

    def test_apply_step_size_source_contains_hook_call(self) -> None:
        # Source-level smoke: the public ``apply_step_size`` entry point's
        # body must invoke ``_maybe_capture_before_manual_device_write``
        # with ``field_key='step_size'`` BEFORE the write path dispatch.
        # Inspecting source avoids the segfault that DPG-less invocation of
        # the full apply chain triggers on Windows at process exit while
        # still proving the wiring. After the drag-storm-debounce work the actual
        # ``settings_service.set_step_size`` call lives in the extracted
        # ``_do_write_step_size`` helper; the dispatch from ``apply_step_size``
        # to that helper is the load-bearing post-hook anchor.
        import inspect
        src = inspect.getsource(self.shell.__class__.apply_step_size)
        hook_idx = src.find("_maybe_capture_before_manual_device_write")
        write_idx = src.find("_do_write_step_size(")
        self.assertGreater(hook_idx, 0, "step_size hook missing")
        self.assertGreater(write_idx, 0, "step_size write dispatch missing")
        self.assertLess(hook_idx, write_idx, "hook must fire BEFORE write")
        self.assertIn("field_key=\"step_size\"", src)

    def test_apply_polling_rate_source_contains_hook_call(self) -> None:
        # See ``test_apply_step_size_source_contains_hook_call`` — same
        # rationale for inspecting the dispatch to ``_do_write_polling_rate``
        # instead of the inner ``settings_service.set_polling_rate`` call.
        import inspect
        src = inspect.getsource(self.shell.__class__.apply_polling_rate)
        hook_idx = src.find("_maybe_capture_before_manual_device_write")
        write_idx = src.find("_do_write_polling_rate(")
        self.assertGreater(hook_idx, 0, "polling_rate hook missing")
        self.assertGreater(write_idx, 0, "polling_rate write dispatch missing")
        self.assertLess(hook_idx, write_idx, "hook must fire BEFORE write")
        self.assertIn("field_key=\"polling_rate\"", src)


# ---------------------------------------------------------------------------
# first_readable_connect
# ---------------------------------------------------------------------------


class FirstReadableConnectHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_first_refresh_fires_first_readable_connect_once(self) -> None:
        # Drive the helper directly — refresh_from_controller's full path
        # requires a real settings_service. The hook is the public surface.
        self.shell._maybe_capture_first_readable_connect()
        self.shell._maybe_capture_first_readable_connect()
        self.shell._maybe_capture_first_readable_connect()
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["first_readable_connect"])

    def test_different_identity_fires_again(self) -> None:
        self.shell._maybe_capture_first_readable_connect()
        # Simulate identity change (different product_string).
        self.shell.device_service.state.product_name = "Other Controller"
        self.shell._maybe_capture_first_readable_connect()
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["first_readable_connect", "first_readable_connect"])


# ---------------------------------------------------------------------------
# Manual button (RPU5)
# ---------------------------------------------------------------------------


class ManualButtonHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell, self.service = _make_shell_with_recording_rp_service()

    def test_manual_save_restore_point_fires_manual_trigger(self) -> None:
        rp = self.shell.manual_save_restore_point()
        self.assertIsNotNone(rp)
        captures = [trigger for trigger, _, _ in self.service.captures]
        self.assertEqual(captures, ["manual"])

    def test_manual_save_restore_point_forwards_title(self) -> None:
        self.shell.manual_save_restore_point(title="My checkpoint")
        triggers_titles = [(trigger, title) for trigger, title, _ in self.service.captures]
        self.assertEqual(triggers_titles, [("manual", "My checkpoint")])

    def test_manual_save_returns_none_without_service(self) -> None:
        self.shell.restore_point_service = None
        result = self.shell.manual_save_restore_point()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Defensive: capture exceptions never escape the apply path
# ---------------------------------------------------------------------------


class CaptureExceptionContainmentTests(unittest.TestCase):
    def test_capture_exception_is_swallowed_by_helper(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        # The hook helper must not raise (the broad except / log path is
        # the load-bearing contract that prevents a transient HID issue
        # from breaking apply / refresh / manual-button flows).
        shell._maybe_capture_before_manual_device_write(field_key="step_size")
        # The debounce timestamp should NOT have been set (capture returned
        # None), so a follow-up call would still attempt capture — that's
        # the intentional retry-on-transient-failure behavior.
        self.assertNotIn("step_size", shell._last_manual_rp_for_field)

    def test_first_connect_helper_swallows_capture_exception(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        shell._maybe_capture_first_readable_connect()
        # The session-set should NOT have grown because capture returned
        # None — same retry-on-transient logic as the manual-write debounce.
        self.assertEqual(shell._first_connect_captured, set())

    def test_manual_save_swallows_capture_exception(self) -> None:
        shell, _ = _make_shell_with_recording_rp_service()
        shell.restore_point_service.capture = MagicMock(side_effect=RuntimeError("hid timeout"))
        result = shell.manual_save_restore_point()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
