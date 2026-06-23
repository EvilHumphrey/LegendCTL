"""Tests for the local-first crash reporter.

Covers excepthook chaining, threading.excepthook capture, faulthandler
enable, the rolling log buffer, and the unread-report listing / mark-as-
reviewed APIs. The native-crash path is not exercised here —
faulthandler is only asserted to be enabled.
"""

from __future__ import annotations

import datetime
import faulthandler
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from zd_app.services import crash_reporter


class CrashReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.user_data_dir = Path(self._tmp.name)
        crash_reporter._reset_for_tests()

    def tearDown(self) -> None:
        crash_reporter._reset_for_tests()
        self._tmp.cleanup()

    def _trigger_excepthook(self, exc: BaseException) -> None:
        try:
            raise exc
        except BaseException:  # noqa: BLE001
            sys.excepthook(*sys.exc_info())

    def _read_only_crash_report(self) -> Path:
        crashes_dir = self.user_data_dir / "crashes"
        reports = sorted(p for p in crashes_dir.iterdir() if p.suffix == ".txt" and not p.name.startswith("_"))
        self.assertEqual(len(reports), 1, f"expected exactly 1 report, found {reports}")
        return reports[0]

    def test_install_crash_handlers_writes_excepthook_report(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self._trigger_excepthook(RuntimeError("test boom"))

        report_path = self._read_only_crash_report()
        body = report_path.read_text(encoding="utf-8")
        self.assertIn("crash report", body)
        self.assertIn("Source:        excepthook", body)
        self.assertIn("Thread:        MainThread", body)
        self.assertIn("RuntimeError: test boom", body)
        # Sanitization: no sys.argv, no env vars in the report.
        self.assertNotIn("sys.argv", body)
        self.assertNotIn("PATH=", body)

    def test_crash_report_scrubs_home_path_in_exception_message(self) -> None:
        # Regression: a path-bearing exception value (and from-source frame
        # paths) used to write C:\Users\<name> into the crash report. The
        # username must be dropped; the basename stays for debuggability.
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self._trigger_excepthook(
            FileNotFoundError(r"C:\Users\Jane Doe\AppData\Roaming\config.json")
        )

        body = self._read_only_crash_report().read_text(encoding="utf-8")
        self.assertNotIn("Jane", body)
        self.assertNotIn("Doe", body)
        self.assertNotIn(r"C:\Users", body)
        # Still diagnostically useful: exception type + the file basename.
        self.assertIn("FileNotFoundError", body)
        self.assertIn("config.json", body)

    def test_install_crash_handlers_chains_previous_excepthook(self) -> None:
        captured: list[tuple] = []

        def previous_hook(exc_type, exc_value, exc_tb) -> None:
            captured.append((exc_type, str(exc_value)))

        sys.excepthook = previous_hook
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self._trigger_excepthook(ValueError("chained"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], ValueError)
        self.assertEqual(captured[0][1], "chained")

    def test_install_crash_handlers_idempotent(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        first_hook = sys.excepthook
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self.assertIs(sys.excepthook, first_hook, "second install must not re-wrap the hook")

    def test_threading_excepthook_writes_report(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)

        def worker() -> None:
            raise RuntimeError("worker boom")

        thread = threading.Thread(target=worker, name="worker-1")
        thread.start()
        thread.join()

        report_path = self._read_only_crash_report()
        body = report_path.read_text(encoding="utf-8")
        self.assertIn("Source:        threading", body)
        self.assertIn("Thread:        worker-1", body)
        self.assertIn("RuntimeError: worker boom", body)

    def test_faulthandler_enabled_after_install(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self.assertTrue(faulthandler.is_enabled())
        self.assertTrue((self.user_data_dir / "crashes" / "_faulthandler.log").exists())

    def test_record_log_entry_buffer_caps_at_50(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        for index in range(75):
            crash_reporter.record_log_entry(f"test.entry.{index}")
        self.assertEqual(len(crash_reporter._log_buffer), 50)
        # Newest 50 are entries 25..74 (FIFO eviction).
        keys = [key for _, key in crash_reporter._log_buffer]
        self.assertEqual(keys[0], "test.entry.25")
        self.assertEqual(keys[-1], "test.entry.74")

    def test_record_log_entry_drops_fmt_args(self) -> None:
        """fmt_args is intentionally not stored — sanitization rule."""
        crash_reporter.install_crash_handlers(self.user_data_dir)
        crash_reporter.record_log_entry(
            "log.config.exported",
            fmt_args={"path": r"C:\Users\real_user\private.json"},
        )
        self._trigger_excepthook(RuntimeError("boom"))
        body = self._read_only_crash_report().read_text(encoding="utf-8")
        self.assertIn("log.config.exported", body)
        self.assertNotIn("real_user", body)
        self.assertNotIn("private.json", body)

    def test_list_unread_crash_reports_filters_by_since(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        crashes_dir = self.user_data_dir / "crashes"

        old_report = crashes_dir / "20250101T000000Z.txt"
        old_report.write_text("old", encoding="utf-8")
        # Backdate mtime so it's clearly older than the threshold.
        old_mtime = time.time() - 86400
        import os
        os.utime(old_report, (old_mtime, old_mtime))

        new_report = crashes_dir / "20260509T120000Z.txt"
        new_report.write_text("new", encoding="utf-8")

        threshold = datetime.datetime.fromtimestamp(time.time() - 3600, tz=datetime.timezone.utc)
        unread = crash_reporter.list_unread_crash_reports(self.user_data_dir, since=threshold)
        names = sorted(p.name for p in unread)
        self.assertEqual(names, ["20260509T120000Z.txt"])

    def test_list_unread_excludes_faulthandler_log_and_reviewed(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        crashes_dir = self.user_data_dir / "crashes"

        report = crashes_dir / "20260509T130000Z.txt"
        report.write_text("body", encoding="utf-8")
        marker = crashes_dir / "20260509T130000Z.txt.reviewed"
        marker.write_text("", encoding="utf-8")

        unread = crash_reporter.list_unread_crash_reports(self.user_data_dir, since=None)
        # _faulthandler.log is excluded by leading-underscore + suffix is .log;
        # the reviewed report is excluded by adjacent marker.
        self.assertEqual(unread, [])

    def test_mark_crashes_reviewed_creates_marker(self) -> None:
        crash_reporter.install_crash_handlers(self.user_data_dir)
        crashes_dir = self.user_data_dir / "crashes"
        report = crashes_dir / "20260509T140000Z.txt"
        report.write_text("body", encoding="utf-8")

        crash_reporter.mark_crashes_reviewed(self.user_data_dir, [report])
        marker = crashes_dir / "20260509T140000Z.txt.reviewed"
        self.assertTrue(marker.exists())

        unread_after = crash_reporter.list_unread_crash_reports(self.user_data_dir, since=None)
        self.assertEqual(unread_after, [])

    def test_two_same_second_crashes_both_preserved(self) -> None:
        """B5: the iso-second basename + truncate-overwrite means two crashes in
        the same wall-clock second collide on one filename and the later writer
        silently clobbers the earlier (often root-cause) report. Both the
        main-thread and worker-thread excepthooks are unsynchronized, so the
        filename must be collision-safe — both reports must survive."""
        crash_reporter.install_crash_handlers(self.user_data_dir)

        fixed = datetime.datetime(2026, 6, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
        real_datetime = crash_reporter.datetime.datetime

        class _FrozenDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D401 - frozen clock for the test
                return fixed

        with mock.patch.object(crash_reporter.datetime, "datetime", _FrozenDateTime):
            self._trigger_excepthook(RuntimeError("first boom"))
            self._trigger_excepthook(RuntimeError("second boom"))

        crashes_dir = self.user_data_dir / "crashes"
        reports = sorted(
            p for p in crashes_dir.iterdir()
            if p.suffix == ".txt" and not p.name.startswith("_")
        )
        self.assertEqual(
            len(reports), 2, f"expected both same-second reports preserved, found {reports}"
        )
        bodies = "\n".join(p.read_text(encoding="utf-8") for p in reports)
        self.assertIn("first boom", bodies)
        self.assertIn("second boom", bodies)

    def test_faulthandler_log_truncated_on_each_startup(self) -> None:
        """The faulthandler log opens in "w" (truncate) per startup, not "a".

        It's written at the C level (unsanitized, raw home paths) and is only
        useful for the crash that just happened, so historical native stacks
        must not accumulate across launches. Two _reset_for_tests()-separated
        installs simulate two process lifetimes. Fail-on-base: with "a" the
        prior content survives the second install.
        """
        crash_reporter.install_crash_handlers(self.user_data_dir)
        log_path = self.user_data_dir / "crashes" / "_faulthandler.log"

        # Simulate a prior crash's native stack (with a home path) landing in
        # the log via the module's held, line-buffered handle.
        sentinel = r"PRIOR-NATIVE-STACK C:\Users\Jane Doe\zd_app\x.py"
        self.assertIsNotNone(crash_reporter._faulthandler_log_handle)
        crash_reporter._faulthandler_log_handle.write(sentinel + "\n")
        crash_reporter._faulthandler_log_handle.flush()
        self.assertIn(sentinel, log_path.read_text(encoding="utf-8"))

        # Fresh process: reset module state, re-install (re-opens the log).
        crash_reporter._reset_for_tests()
        crash_reporter.install_crash_handlers(self.user_data_dir)

        after = log_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, after)
        self.assertNotIn("Jane Doe", after)
        self.assertEqual(after, "")  # truncated clean on the new startup

    def test_install_writes_readme_marker_excluded_from_unread(self) -> None:
        """install writes a _README.txt privacy marker that warns the folder
        is not share-safe, and the marker never surfaces as an unread report
        (leading-underscore convention). Fail-on-base: no marker is written.
        """
        crash_reporter.install_crash_handlers(self.user_data_dir)
        readme = self.user_data_dir / "crashes" / "_README.txt"
        self.assertTrue(readme.exists())
        text = readme.read_text(encoding="utf-8")
        self.assertIn("NOT SHARE-SAFE", text)
        self.assertIn("_faulthandler.log", text)

        # The marker (and the faulthandler log) must not be treated as reports.
        unread = crash_reporter.list_unread_crash_reports(self.user_data_dir, since=None)
        self.assertEqual(unread, [])

    def test_diagnostic_bundle_excludes_crash_artifacts(self) -> None:
        """Lock the bound: crash artifacts (_faulthandler.log, .dmp, .txt
        reports) live under <user_data>/crashes/ and must never be packed into
        the shareable diagnostic bundle. Guards a future change from slipping
        them into the bundle's namelist.
        """
        import zipfile

        from zd_app.services.diagnostic_bundle import DiagnosticBundleService

        bundle_dir = self.user_data_dir / "bundles"
        bundle = DiagnosticBundleService(base_dir=bundle_dir)
        out = bundle.generate_bundle_zip(bundle_dir / "bundle.zip")
        self.assertIsNotNone(out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        self.assertTrue(names)  # bundle is non-empty (report.md at minimum)
        for name in names:
            lowered = name.lower()
            self.assertNotIn("crash", lowered)
            self.assertNotIn("faulthandler", lowered)


class CrashReporterNativeWindowsHookTests(unittest.TestCase):
    """The native SEH hook was reverted after a DPG-import-segfault regression.

    The hook function body is preserved in crash_reporter.py for future
    re-enablement. These tests pin the deferred-state contract — the
    production install path must NOT call _install_native_windows_hook —
    and exercise the function directly so the code stays warm.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.user_data_dir = Path(self._tmp.name)
        crash_reporter._reset_for_tests()

    def tearDown(self) -> None:
        crash_reporter._reset_for_tests()
        self._tmp.cleanup()

    def test_native_hook_deferred_to_phase_1b(self) -> None:
        """The native SEH hook was reverted after a DPG-import-segfault regression.

        The hook's install path remains in the module for future re-enablement.
        For now, install_crash_handlers() must NOT call it —
        install_crash_handlers should leave _native_hook_installed False.
        """
        crash_reporter.install_crash_handlers(self.user_data_dir)
        self.assertFalse(
            crash_reporter._native_hook_installed,
            "Native SEH hook must stay deferred (see crash_reporter.py comment)",
        )
        self.assertIsNone(
            crash_reporter._native_filter_callback,
            "no SEH callback should be retained when the hook is deferred",
        )

    @unittest.skipUnless(sys.platform == "win32", "native hook is Windows-only")
    def test_native_hook_function_body_idempotent_when_invoked_directly(self) -> None:
        """Smoke that _install_native_windows_hook itself stays correct for future re-enablement.

        The function body is preserved so re-enablement is gated on the
        _MINIDUMP_OPT_IN tripwire (not a bare uncomment). This test opts in
        explicitly to exercise the body directly (bypassing the deferred
        install_crash_handlers gate) and verify the idempotency contract
        survives the revert.
        """
        crashes_dir = self.user_data_dir / "crashes"
        crashes_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(crash_reporter, "_MINIDUMP_OPT_IN", True):
            crash_reporter._install_native_windows_hook(crashes_dir)
            first_callback = crash_reporter._native_filter_callback
            crash_reporter._install_native_windows_hook(crashes_dir)
        self.assertIsNotNone(first_callback)
        self.assertIs(crash_reporter._native_filter_callback, first_callback)

    def test_native_hook_inert_without_opt_in_tripwire(self) -> None:
        """The _MINIDUMP_OPT_IN tripwire keeps the minidump hook inert by default.

        Even invoked directly (bypassing the deferred install_crash_handlers
        gate), the hook must not arm unless _MINIDUMP_OPT_IN is explicitly
        flipped — uncommenting the deferred call site alone is insufficient.
        Holds on every platform: the tripwire returns before any Windows-only
        ctypes work.
        """
        self.assertFalse(crash_reporter._MINIDUMP_OPT_IN)
        crashes_dir = self.user_data_dir / "crashes"
        crashes_dir.mkdir(parents=True, exist_ok=True)
        crash_reporter._install_native_windows_hook(crashes_dir)
        self.assertFalse(crash_reporter._native_hook_installed)
        self.assertIsNone(crash_reporter._native_filter_callback)


class CrashReporterDeviceServiceIntegrationTests(unittest.TestCase):
    """DeviceService.log_i18n_event feeds the crash buffer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.user_data_dir = Path(self._tmp.name)
        crash_reporter._reset_for_tests()

    def tearDown(self) -> None:
        crash_reporter._reset_for_tests()
        self._tmp.cleanup()

    def test_device_service_log_event_populates_crash_buffer(self) -> None:
        from zd_app.services.device_service import DeviceService

        crash_reporter.install_crash_handlers(self.user_data_dir)

        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event("log.controller.detected", product_name="X", connection_mode="USB")
        service.log_i18n_event("log.snapshot.refreshed_ok")

        keys = [key for _, key in crash_reporter._log_buffer]
        self.assertEqual(keys, ["log.controller.detected", "log.snapshot.refreshed_ok"])

    def test_crash_report_after_device_service_logs_includes_buffer(self) -> None:
        from zd_app.services.device_service import DeviceService

        crash_reporter.install_crash_handlers(self.user_data_dir)
        service = DeviceService(clock=lambda: 0.0)
        service.log_i18n_event("log.controller.detected", product_name="leaky-name", connection_mode="USB")

        try:
            raise RuntimeError("integration boom")
        except BaseException:  # noqa: BLE001
            sys.excepthook(*sys.exc_info())

        report = sorted(
            p for p in (self.user_data_dir / "crashes").iterdir()
            if p.suffix == ".txt" and not p.name.startswith("_")
        )[0]
        body = report.read_text(encoding="utf-8")
        self.assertIn("log.controller.detected", body)
        self.assertIn("RuntimeError: integration boom", body)
        # Sanitization: user input from fmt_args must not leak.
        self.assertNotIn("leaky-name", body)


if __name__ == "__main__":
    unittest.main()
