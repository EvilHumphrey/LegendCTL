"""End-to-end wiring tests for main_zd.main() stock-mode boot.

The default entry point no longer auto-runs the v1 dual-mode trigger.
These tests mock SettingsService + AppShell + downstream constructors so
main_zd's control-flow is exercised without touching Win32, real ctypes,
or the AppShell render loop.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from zd_app.models import AppSettings
from zd_app.services.settings_service import SetPollingRateOutcome


def _make_settings_service_mock(
    *, start_result: SetPollingRateOutcome = SetPollingRateOutcome.OK
) -> MagicMock:
    """Build a mock SettingsService with a configurable start() outcome."""
    mock = MagicMock()
    mock.start.return_value = start_result
    return mock


def _patches(
    *,
    settings_service_mock: MagicMock | None = None,
    settings: AppSettings | None = None,
):
    """Patch set used by every test: settings + services + AppShell."""
    settings = settings or AppSettings()
    settings_service_mock = settings_service_mock or _make_settings_service_mock()
    settings_store_mock = MagicMock()
    settings_store_mock.load.return_value = settings

    return [
        patch("main_zd.SettingsStore", return_value=settings_store_mock),
        patch("main_zd.SettingsService", return_value=settings_service_mock),
        patch("main_zd.AppShell"),
        patch("main_zd.DeviceService"),
        patch("main_zd.ProfileService"),
        patch("main_zd.ProfileStore"),
        patch("main_zd.DiagnosticsService"),
        patch("main_zd._warn_if_vmware_usb_redirect_active"),
    ]


class _StopAfterWaits:
    def __init__(self, retry_count: int):
        self.retry_count = retry_count
        self.wait_calls = 0

    def wait(self, _interval: float) -> bool:
        if self.wait_calls >= self.retry_count:
            return True
        self.wait_calls += 1
        return False


class TestMainStockModeBoot(unittest.TestCase):
    def test_main_module_does_not_wire_standalone_trigger_service(self) -> None:
        import main_zd

        self.assertFalse(hasattr(main_zd, "StandaloneTriggerService"))
        self.assertFalse(hasattr(main_zd, "StandaloneStartStatus"))

    def test_main_does_not_instantiate_standalone_trigger_service(self) -> None:
        settings_service = _make_settings_service_mock()
        app_shell_instance = MagicMock()

        with patch(
            "zd_app.services.standalone_trigger_service.StandaloneTriggerService",
            side_effect=AssertionError("default boot must not trigger dual-mode"),
        ) as trigger_constructor:
            with ExitStack() as stack:
                for p in _patches(settings_service_mock=settings_service):
                    stack.enter_context(p)
                from main_zd import AppShell

                AppShell.return_value = app_shell_instance
                from main_zd import main

                self.assertEqual(main(), 0)

        trigger_constructor.assert_not_called()

    def test_started_runs_app_shell_and_stops_settings_service(self) -> None:
        settings_service = _make_settings_service_mock()
        app_shell_instance = MagicMock()
        app_shell_kwargs = {}

        def make_app_shell(**kwargs):
            app_shell_kwargs.update(kwargs)
            return app_shell_instance

        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            from main_zd import AppShell

            AppShell.side_effect = make_app_shell
            from main_zd import main

            self.assertEqual(main(), 0)

        settings_service.start.assert_called_once()
        settings_service.stop.assert_called_once()
        app_shell_instance.run.assert_called_once()
        self.assertIs(app_shell_kwargs["settings_service"], settings_service)

    def test_settings_service_start_failure_is_soft_failed(self) -> None:
        settings_service = _make_settings_service_mock(
            start_result=SetPollingRateOutcome.DEVICE_NOT_FOUND
        )
        app_shell_instance = MagicMock()
        app_shell_kwargs = {}

        def make_app_shell(**kwargs):
            app_shell_kwargs.update(kwargs)
            return app_shell_instance

        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            from main_zd import AppShell

            AppShell.side_effect = make_app_shell
            from main_zd import main

            self.assertEqual(main(), 0)

        settings_service.start.assert_called_once()
        self.assertIsNone(app_shell_kwargs["settings_service"])
        app_shell_instance.run.assert_called_once()
        settings_service.stop.assert_called_once()

    def test_start_failure_starts_settings_service_watchdog(self) -> None:
        settings_service = _make_settings_service_mock(
            start_result=SetPollingRateOutcome.DEVICE_NOT_FOUND
        )
        app_shell_instance = MagicMock()
        thread_instance = MagicMock()
        stop_event = MagicMock()

        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            stack.enter_context(patch("main_zd.threading.Thread", return_value=thread_instance))
            stack.enter_context(patch("main_zd.threading.Event", return_value=stop_event))
            from main_zd import main

            from main_zd import AppShell

            AppShell.return_value = app_shell_instance

            self.assertEqual(main(), 0)

        thread_instance.start.assert_called_once_with()
        stop_event.set.assert_called_once_with()
        thread_instance.join.assert_called_once_with(timeout=3.0)

    def test_watchdog_starts_settings_service_when_initially_unavailable(self) -> None:
        from main_zd import _settings_service_watchdog

        settings_service = MagicMock()
        settings_service.start.side_effect = [
            SetPollingRateOutcome.DEVICE_NOT_FOUND,
            SetPollingRateOutcome.OK,
        ]
        shell = MagicMock()
        shell.settings_service = None

        _settings_service_watchdog(
            shell,
            settings_service,
            _StopAfterWaits(retry_count=2),
            retry_interval=0.0,
        )

        self.assertEqual(settings_service.start.call_count, 2)
        # Late-connect wiring goes through the single attach entry point — it
        # rebinds the apply coordinator + builds the RestorePointService, not
        # just shell.settings_service (2026-06-09 review, Bug 1).
        shell.attach_settings_service.assert_called_once_with(settings_service)


class TestMainShutdownOnException(unittest.TestCase):
    def test_settings_service_stop_called_when_app_shell_raises(self) -> None:
        settings_service = _make_settings_service_mock()
        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            from main_zd import AppShell

            instance = MagicMock()
            instance.run.side_effect = RuntimeError("UI crashed")
            AppShell.return_value = instance
            from main_zd import main

            with self.assertRaises(RuntimeError):
                main()

        settings_service.stop.assert_called_once()

    def test_settings_service_stop_called_when_warn_function_raises(self) -> None:
        settings_service = _make_settings_service_mock()
        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            from main_zd import _warn_if_vmware_usb_redirect_active

            _warn_if_vmware_usb_redirect_active.side_effect = OSError("fake")
            from main_zd import main

            with self.assertRaises(OSError):
                main()

        settings_service.stop.assert_called_once()


class TestMainServiceConstructorArgs(unittest.TestCase):
    def test_settings_service_constructed_with_default_args(self) -> None:
        with ExitStack() as stack:
            for p in _patches():
                stack.enter_context(p)
            from main_zd import SettingsService
            from main_zd import main

            main()

        SettingsService.assert_called_once_with()


class TestMainVMwareWarnIsCalled(unittest.TestCase):
    def test_warn_called_after_settings_start_before_app_shell(self) -> None:
        settings_service = _make_settings_service_mock()
        call_log: list[str] = []
        settings_service.start.side_effect = lambda: (
            call_log.append("settings_start") or SetPollingRateOutcome.OK
        )

        def warn():
            call_log.append("warn")

        def app_shell_init(**kwargs):
            call_log.append("app_shell_init")
            instance = MagicMock()
            instance.run.side_effect = lambda: call_log.append("app_shell_run")
            return instance

        with ExitStack() as stack:
            for p in _patches(settings_service_mock=settings_service):
                stack.enter_context(p)
            from main_zd import _warn_if_vmware_usb_redirect_active

            _warn_if_vmware_usb_redirect_active.side_effect = warn
            from main_zd import AppShell

            AppShell.side_effect = app_shell_init
            from main_zd import main

            self.assertEqual(main(), 0)

        self.assertEqual(
            call_log,
            ["settings_start", "warn", "app_shell_init", "app_shell_run"],
        )


if __name__ == "__main__":
    unittest.main()
