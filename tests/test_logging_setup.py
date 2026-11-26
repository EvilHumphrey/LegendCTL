"""Tests for single-owner rotating file logging, verbosity wiring, and output guards."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zd_app.models import AppSettings
from zd_app.services import crash_reporter, logging_setup, trust_self_check
from zd_app.services.wear_ledger.service import WearLedgerService
from zd_app.storage._output_guards import (
    GuardedOutputDirectory,
    UnsafeOutputPathError,
    guarded_output_directory,
)


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_logger = logging.Logger("test.logging_setup.root")
        self.test_logger.setLevel(logging.INFO)
        logging_setup.shutdown_file_logging(root_logger=self.test_logger)

    def tearDown(self) -> None:
        logging_setup.shutdown_file_logging(root_logger=self.test_logger)

    def test_off_mode_never_creates_logs_dir_and_discards_startup_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            logging_setup.begin_startup_logging(root_logger=self.test_logger)
            self.test_logger.warning("early startup warning")

            active = logging_setup.apply_logging_verbosity(
                "Off",
                user_data_dir=data_dir,
                root_logger=self.test_logger,
            )
            self.assertFalse(active)
            self.assertFalse(logging_setup.is_file_logging_active())
            self.assertFalse((data_dir / "logs").exists())

    def test_normal_mode_flushes_startup_buffer_emits_banner_and_filters_debug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            logging_setup.begin_startup_logging(root_logger=self.test_logger)
            self.test_logger.debug("buffered debug should stay out of Normal")
            self.test_logger.warning("buffered warning should land")

            active = logging_setup.apply_logging_verbosity(
                "Normal",
                user_data_dir=data_dir,
                root_logger=self.test_logger,
            )
            self.assertTrue(active)
            self.test_logger.debug("live debug hidden in Normal")
            self.test_logger.info("live info visible in Normal")
            logging_setup.shutdown_file_logging(root_logger=self.test_logger)

            text = (data_dir / "logs" / "zd_wrapper.log").read_text(encoding="utf-8")
            self.assertIn("LegendCTL startup:", text)
            self.assertIn("logging_verbosity=Normal", text)
            self.assertIn("buffered warning should land", text)
            self.assertIn("live info visible in Normal", text)
            self.assertNotIn("buffered debug should stay out of Normal", text)
            self.assertNotIn("live debug hidden in Normal", text)

    def test_live_transitions_between_quiet_verbose_and_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            self.assertTrue(
                logging_setup.apply_logging_verbosity(
                    "Quiet",
                    user_data_dir=data_dir,
                    root_logger=self.test_logger,
                )
            )
            self.test_logger.info("quiet hides info")
            self.test_logger.warning("quiet records warning")

            self.assertTrue(
                logging_setup.apply_logging_verbosity(
                    "Verbose",
                    root_logger=self.test_logger,
                )
            )
            self.test_logger.debug("verbose records debug")

            self.assertFalse(
                logging_setup.apply_logging_verbosity(
                    "Off",
                    root_logger=self.test_logger,
                )
            )
            self.test_logger.error("off suppresses file writes")

            # Re-enable after Off attaches a fresh handler and emits banner.
            self.assertTrue(
                logging_setup.apply_logging_verbosity(
                    "Normal",
                    root_logger=self.test_logger,
                )
            )
            self.test_logger.info("re-enabled info")
            logging_setup.shutdown_file_logging(root_logger=self.test_logger)

            text = (data_dir / "logs" / "zd_wrapper.log").read_text(encoding="utf-8")
            self.assertNotIn("quiet hides info", text)
            self.assertIn("quiet records warning", text)
            self.assertIn("verbose records debug", text)
            self.assertNotIn("off suppresses file writes", text)
            self.assertIn("LegendCTL startup:", text)
            self.assertIn("re-enabled info", text)

    def test_invalid_logging_verbosity_normalizes_to_normal(self) -> None:
        settings = AppSettings.from_dict({"logging_verbosity": "Bogus"})
        self.assertEqual(settings.logging_verbosity, "Normal")
        settings_off = AppSettings.from_dict({"logging_verbosity": "Off"})
        self.assertEqual(settings_off.logging_verbosity, "Off")

    def test_rollover_rotates_backups_and_enforces_hard_cap_when_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            handler = logging_setup.GuardedRotatingFileHandler(
                data_dir,
                maxBytes=120,
                backupCount=2,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            try:
                for idx in range(10):
                    rec = self.test_logger.makeRecord(
                        "t", logging.INFO, "", 0, f"line-{idx}-" + ("x" * 60), (), None
                    )
                    handler.emit(rec)
                self.assertTrue((data_dir / "logs" / "zd_wrapper.log.1").is_file())
                self.assertFalse((data_dir / "logs" / "zd_wrapper.log.3").exists())

                # Simulate a Windows rollover sharing error and confirm hard cap holds
                # without thrashing _open() on every dropped record or dropping
                # existing backups on retry.
                with mock.patch(
                    "zd_app.storage._output_guards.GuardedOutputDirectory.rename_noreplace",
                    side_effect=PermissionError("locked"),
                ):
                    with mock.patch.object(
                        handler, "_open", wraps=handler._open
                    ) as open_spy:
                        for idx in range(20):
                            rec = self.test_logger.makeRecord(
                                "t", logging.INFO, "", 0, f"blocked-{idx}-" + ("y" * 80), (), None
                            )
                            handler.emit(rec)
                        self.assertLessEqual(open_spy.call_count, 2)
                size_after = (data_dir / "logs" / "zd_wrapper.log").stat().st_size
                self.assertLess(size_after, 512)
                self.assertTrue((data_dir / "logs" / "zd_wrapper.log.1").is_file())
            finally:
                handler.close()

    def test_root_logger_and_stream_handler_do_not_leak_debug_after_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            captured: list[logging.LogRecord] = []

            class _ListHandler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    captured.append(record)

            stream_like = _ListHandler()  # level == NOTSET, like basicConfig StreamHandler
            self.test_logger.setLevel(logging.INFO)
            self.test_logger.addHandler(stream_like)
            try:
                logging_setup.begin_startup_logging(root_logger=self.test_logger, capacity=4)
                for idx in range(10):
                    self.test_logger.debug("buffered-debug-%d", idx)
                # Pre-existing NOTSET handler must not receive DEBUG during startup buffering.
                self.assertEqual(captured, [])
                buf = logging_setup._startup_buffer_handler
                self.assertIsNotNone(buf)
                self.assertEqual(len(buf.buffer), 4)

                logging_setup.apply_logging_verbosity(
                    "Off", user_data_dir=data_dir, root_logger=self.test_logger
                )
                self.assertEqual(self.test_logger.level, logging.INFO)
                self.test_logger.debug("must not leak in Off")
                self.assertEqual(captured, [])

                logging_setup.apply_logging_verbosity(
                    "Normal", user_data_dir=data_dir, root_logger=self.test_logger
                )
                self.assertEqual(self.test_logger.level, logging.INFO)
                self.test_logger.debug("must not leak in Normal")
                self.assertEqual(captured, [])

                logging_setup.apply_logging_verbosity(
                    "Verbose", user_data_dir=data_dir, root_logger=self.test_logger
                )
                self.assertEqual(self.test_logger.level, logging.DEBUG)
                self.test_logger.debug("file only debug in Verbose")
                self.assertEqual(captured, [])

                logging_setup.shutdown_file_logging(root_logger=self.test_logger)
                self.assertEqual(self.test_logger.level, logging.INFO)
                self.assertEqual(stream_like.level, logging.NOTSET)
            finally:
                self.test_logger.removeHandler(stream_like)

    def test_shutdown_restores_original_notset_root_level(self) -> None:
        notset_logger = logging.Logger("test.logging_setup.notset")
        notset_logger.setLevel(logging.NOTSET)
        logging_setup.begin_startup_logging(root_logger=notset_logger)
        self.assertEqual(notset_logger.level, logging.DEBUG)
        logging_setup.shutdown_file_logging(root_logger=notset_logger)
        self.assertEqual(notset_logger.level, logging.NOTSET)

    def test_closed_handler_never_reopens_on_late_emit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            handler = logging_setup.GuardedRotatingFileHandler(data_dir, maxBytes=1024, backupCount=2)
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.close()
            size_before = (data_dir / "logs" / "zd_wrapper.log").stat().st_size
            rec = self.test_logger.makeRecord("t", logging.ERROR, "", 0, "late-after-close", (), None)
            handler.emit(rec)
            self.assertIsNone(handler.stream)
            self.assertEqual((data_dir / "logs" / "zd_wrapper.log").stat().st_size, size_before)


class OutputGuardHardeningTests(unittest.TestCase):
    def test_allow_fallback_false_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            with self.assertRaises(UnsafeOutputPathError):
                with guarded_output_directory(outside, root, allow_fallback=False):
                    pass

    def test_open_binary_create_and_append_work_in_pinned_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "out"
            with guarded_output_directory(root, root, allow_fallback=False) as output:
                stream, path = output.open_binary_create("sample.bin")
                with stream:
                    stream.write(b"abc")
                self.assertEqual(path.read_bytes(), b"abc")
                with output.open_binary_append("sample.bin") as app:
                    app.write(b"def")
                self.assertEqual(path.read_bytes(), b"abcdef")

    def test_handle_bound_read_rename_noreplace_and_unlink_reject_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "out"
            with guarded_output_directory(
                root, root, allow_fallback=False, allow_rename=True
            ) as output:
                p = output.write_text("orig.txt", "data")
                output.write_text("existing.txt", "keep-me")
                alias = root / "hardlink.txt"
                os.link(p, alias)
                with self.assertRaises(UnsafeOutputPathError):
                    output.open_binary_read("orig.txt")
                with self.assertRaises(UnsafeOutputPathError):
                    output.rename_noreplace("orig.txt", "moved.txt")
                with self.assertRaises(UnsafeOutputPathError):
                    output.unlink_file("orig.txt")
                os.unlink(alias)
                # Existing destination must never be deleted or overwritten.
                with self.assertRaises(FileExistsError):
                    output.rename_noreplace("orig.txt", "existing.txt")
                self.assertEqual((root / "orig.txt").read_text(encoding="utf-8"), "data")
                self.assertEqual((root / "existing.txt").read_text(encoding="utf-8"), "keep-me")
                # Now single-link to absent destination: read, rename, and unlink succeed.
                with output.open_binary_read("orig.txt") as fh:
                    self.assertEqual(fh.read(), b"data")
                output.rename_noreplace("orig.txt", "moved.txt")
                self.assertFalse((root / "orig.txt").exists())
                self.assertEqual((root / "moved.txt").read_text(encoding="utf-8"), "data")
                output.unlink_file("moved.txt")
                self.assertFalse((root / "moved.txt").exists())

    def test_rollover_preserves_backups_when_active_rename_fails_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            logs_dir = data_dir / "logs"
            logs_dir.mkdir()
            (logs_dir / "zd_wrapper.log").write_text("active-content\n", encoding="utf-8")
            (logs_dir / "zd_wrapper.log.1").write_text("backup-1\n", encoding="utf-8")
            (logs_dir / "zd_wrapper.log.2").write_text("backup-2\n", encoding="utf-8")

            handler = logging_setup.GuardedRotatingFileHandler(
                data_dir,
                maxBytes=10,
                backupCount=2,
            )
            try:
                orig_rename = GuardedOutputDirectory.rename_noreplace

                def fail_active(self_dir, src_name, dst_name):
                    if src_name == "zd_wrapper.log":
                        raise PermissionError("active locked")
                    return orig_rename(self_dir, src_name, dst_name)

                with mock.patch(
                    "zd_app.storage._output_guards.GuardedOutputDirectory.rename_noreplace",
                    autospec=True,
                    side_effect=fail_active,
                ):
                    handler.doRollover()
                    # .2 expired, .1 shifted to .2, active stayed intact, .1 is now free.
                    self.assertEqual((logs_dir / "zd_wrapper.log.2").read_text(encoding="utf-8"), "backup-1\n")
                    self.assertFalse((logs_dir / "zd_wrapper.log.1").exists())
                    self.assertEqual((logs_dir / "zd_wrapper.log").read_text(encoding="utf-8"), "active-content\n")

                    # A second failed retry must NOT unlink .2 because .1 is free!
                    handler.doRollover()
                    self.assertEqual((logs_dir / "zd_wrapper.log.2").read_text(encoding="utf-8"), "backup-1\n")

                # Once the lock clears, retry moves active into the vacant .1 slot
                # while keeping .2 intact.
                handler.doRollover()
                self.assertEqual((logs_dir / "zd_wrapper.log.2").read_text(encoding="utf-8"), "backup-1\n")
                self.assertEqual((logs_dir / "zd_wrapper.log.1").read_text(encoding="utf-8"), "active-content\n")
            finally:
                handler.close()

    def test_reopen_failure_after_rollover_retries_reopen_without_rotating_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "ZDUltimateLegend"
            data_dir.mkdir()
            logs_dir = data_dir / "logs"
            logs_dir.mkdir()
            (logs_dir / "zd_wrapper.log").write_text("active-content\n", encoding="utf-8")
            (logs_dir / "zd_wrapper.log.1").write_text("backup-1\n", encoding="utf-8")
            (logs_dir / "zd_wrapper.log.2").write_text("backup-2\n", encoding="utf-8")

            handler = logging_setup.GuardedRotatingFileHandler(
                data_dir,
                maxBytes=10,
                backupCount=2,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            try:
                # Fail reopen once after rollover rotates active -> .1 and .1 -> .2.
                with mock.patch.object(
                    handler, "_open", side_effect=PermissionError("reopen failed")
                ):
                    handler.doRollover()
                self.assertFalse(handler._rollover_blocked)
                self.assertTrue(handler._reopen_blocked)
                self.assertIsNone(handler.stream)
                self.assertEqual((logs_dir / "zd_wrapper.log.2").read_text(encoding="utf-8"), "backup-1\n")
                self.assertEqual((logs_dir / "zd_wrapper.log.1").read_text(encoding="utf-8"), "active-content\n")
                self.assertFalse((logs_dir / "zd_wrapper.log").exists())

                # Expire retry backoff and emit: must reopen directly WITHOUT rotating
                # again or deleting .2!
                handler._rollover_retry_after = 0.0
                logger = logging.Logger("test.reopen.retry")
                rec = logger.makeRecord("t", logging.INFO, "", 0, "fresh-line", (), None)
                with mock.patch.object(
                    handler, "doRollover", wraps=handler.doRollover
                ) as rollover_spy:
                    handler.emit(rec)
                    self.assertEqual(rollover_spy.call_count, 0)
                self.assertFalse(handler._reopen_blocked)
                self.assertEqual((logs_dir / "zd_wrapper.log.2").read_text(encoding="utf-8"), "backup-1\n")
                self.assertEqual((logs_dir / "zd_wrapper.log.1").read_text(encoding="utf-8"), "active-content\n")
                self.assertIn("fresh-line", (logs_dir / "zd_wrapper.log").read_text(encoding="utf-8"))
            finally:
                handler.close()


class TrustSelfCheckFindingsPrecedenceTests(unittest.TestCase):
    def test_network_findings_never_emit_clean_no_network_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "zd_app"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "bad.py").write_text("import socket\n", encoding="utf-8")
            result = trust_self_check.build_trust_self_check(package_root=pkg)
            net_row = next(r for r in result.rows if r.key == "network")
            self.assertNotIn("No network:", net_row.claim)
            self.assertIn("Networking imports detected:", net_row.claim)
            self.assertIn("bad.py:1 imports socket", net_row.evidence)

    def test_frozen_live_finding_overrides_clean_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "zd_app"
            pkg.mkdir()
            (pkg / "bad.py").write_text("import socket\n", encoding="utf-8")
            (pkg / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            fake_manifest = SimpleNamespace(
                integrity=SimpleNamespace(valid=True),
                build_commit_short="abc1234",
            )
            fake_verify = SimpleNamespace(clean=True)
            with mock.patch.object(
                trust_self_check, "load_trust_manifest", return_value=fake_manifest
            ), mock.patch.object(
                trust_self_check, "verify_payload_against_manifest", return_value=fake_verify
            ), mock.patch.object(
                trust_self_check.app_version, "__build_commit__", "abc1234"
            ), mock.patch.object(
                trust_self_check, "_manifest_driver_evidence", return_value="manifest-drivers"
            ), mock.patch.object(
                trust_self_check, "_recorded_executable_hash", return_value=None
            ):
                result = trust_self_check.build_trust_self_check(
                    package_root=pkg,
                    frozen=True,
                )
            net_row = next(r for r in result.rows if r.key == "network")
            self.assertNotIn("No network:", net_row.claim)
            self.assertIn("Networking imports detected:", net_row.claim)
            self.assertIn("bad.py:1 imports socket", net_row.evidence)

    def test_driver_findings_never_emit_clean_no_drivers_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "zd_app"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "fake.sys").write_bytes(b"driver")
            result = trust_self_check.build_trust_self_check(package_root=pkg)
            drv_row = next(r for r in result.rows if r.key == "drivers")
            self.assertNotIn("No drivers / virtual devices:", drv_row.claim)
            self.assertIn("Driver or virtual-device artifacts detected:", drv_row.claim)
            self.assertIn("fake.sys", drv_row.evidence)


class CrashAndWearRetentionTests(unittest.TestCase):
    def test_crash_reporter_prunes_only_oldest_reviewed_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "ZDUltimateLegend"
            crashes = user_dir / "crashes"
            crashes.mkdir(parents=True)
            (crashes / "_faulthandler.log").write_text("fault", encoding="utf-8")
            (crashes / "_README.txt").write_text("readme", encoding="utf-8")
            unread = crashes / "20260101T000000Z.txt"
            unread.write_text("unread", encoding="utf-8")
            for idx in range(55):
                name = f"20260201T00{idx:02d}00Z.txt"
                (crashes / name).write_text("reviewed", encoding="utf-8")
                (crashes / (name + ".reviewed")).write_text("", encoding="utf-8")

            pruned = crash_reporter._prune_reviewed_crash_pairs(crashes, keep=50)
            self.assertEqual(pruned, 5)
            self.assertTrue(unread.is_file())
            self.assertTrue((crashes / "_faulthandler.log").is_file())
            self.assertTrue((crashes / "_README.txt").is_file())
            remaining_reviewed = sorted(crashes.glob("20260201T*.txt"))
            remaining_markers = sorted(crashes.glob("20260201T*.txt.reviewed"))
            self.assertEqual(len(remaining_reviewed), 50)
            self.assertEqual(len(remaining_markers), 50)
            self.assertEqual(remaining_reviewed[0].name, "20260201T000500Z.txt")

    def test_wear_ledger_prunes_old_archives_and_skips_oversized_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "wear_ledger"
            clock = [datetime(2026, 11, 25, 0, 0, 0, tzinfo=timezone.utc)]

            def _now() -> datetime:
                clock[0] += timedelta(seconds=5)
                return clock[0]

            svc = WearLedgerService(
                base_dir=base,
                utc_now=_now,
                rotation_bytes=256,
                max_rotated_archives=3,
            )
            for idx in range(15):
                svc.append("test_event", summary=f"event-{idx}-" + ("a" * 160))

            archives = list(base.glob("events-*.jsonl"))
            self.assertLessEqual(len(archives), 3)
            self.assertGreaterEqual(len(archives), 1)

            # Append a line larger than MAX_READ_LINE_BYTES and ensure read stays bounded.
            with open(svc.active_path, "ab") as fh:
                fh.write(b'{"oversized":"' + (b"z" * (70 * 1024)) + b'"}\n')
            events = svc.read_events()
            self.assertGreater(len(events), 0)

    def test_wear_ledger_same_second_pruning_keeps_newest_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "wear_ledger"
            fixed = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
            svc = WearLedgerService(
                base_dir=base,
                utc_now=lambda: fixed,
                rotation_bytes=256,
                max_rotated_archives=2,
            )
            for idx in range(8):
                svc.append("test_event", summary=f"seq-{idx:02d}-" + ("x" * 200))
            remaining = svc.read_events()
            summaries = [e.summary for e in remaining]
            # Newest events (seq-07, seq-06, seq-05) must survive; oldest must be pruned.
            self.assertTrue(any("seq-07-" in s for s in summaries), summaries)
            self.assertTrue(any("seq-06-" in s for s in summaries), summaries)
            self.assertFalse(any("seq-00-" in s for s in summaries), summaries)


class ShellAndRestoreExportHardeningTests(unittest.TestCase):
    def test_shell_update_setting_and_restore_defaults_apply_logging_verbosity(self) -> None:
        from zd_app.ui.app_shell import AppShell

        shell = AppShell.__new__(AppShell)
        shell.settings = AppSettings(logging_verbosity="Normal")
        shell.settings_store = mock.MagicMock()
        shell.device_service = mock.MagicMock()
        shell.refresh_shell = mock.MagicMock()
        shell._locale_router = mock.MagicMock()
        shell.rebuild_full_ui = mock.MagicMock()

        with mock.patch("zd_app.services.logging_setup.apply_logging_verbosity") as apply_mock:
            shell.update_setting("logging_verbosity", "Off")
            self.assertEqual(shell.settings.logging_verbosity, "Off")
            apply_mock.assert_called_once_with("Off")

            # Turning logging Off must still apply even if settings save fails (e.g. disk full).
            apply_mock.reset_mock()
            shell.settings.logging_verbosity = "Normal"
            shell.settings_store.save.side_effect = OSError("disk full")
            shell.update_setting("logging_verbosity", "Off")
            self.assertEqual(shell.settings.logging_verbosity, "Off")
            apply_mock.assert_called_once_with("Off")
            shell.settings_store.save.side_effect = None

            apply_mock.reset_mock()
            shell.update_setting("logging_verbosity", "NotARealLevel")
            self.assertEqual(shell.settings.logging_verbosity, "Normal")
            apply_mock.assert_called_once_with("Normal")

            apply_mock.reset_mock()
            shell.settings.logging_verbosity = "Off"
            shell.restore_app_defaults()
            self.assertEqual(shell.settings.logging_verbosity, "Normal")
            apply_mock.assert_called_once_with("Normal")

    def test_render_active_apply_status_does_not_log_profile_name_or_error(self) -> None:
        from zd_app.ui.app_shell import AppShell

        shell = AppShell.__new__(AppShell)
        shell._dpg_context_ready = True
        shell._apply_status_message = "Saved profile 'My Secret Profile Name'"
        shell._apply_status_success = True
        shell._set_if_exists = mock.MagicMock()

        with self.assertLogs("zd_app.ui.app_shell", level="DEBUG") as logs:
            shell._render_active_apply_status()
        joined = "\n".join(logs.output)
        self.assertNotIn("My Secret Profile Name", joined)

    def test_restore_result_save_sanitizes_home_paths_and_instance_ids(self) -> None:
        from zd_app.storage.restore_point_models import RestoreResultLabel
        from zd_app.ui.screens import restore_points

        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "exports"
            outcome = SimpleNamespace(
                field_name="polling_rate",
                write_succeeded=False,
                write_error=r"Failed at C:\Users\Jane Doe\secret.bin on HID\VID_20BC&PID_5501\7&12345678&0&0000",
                verify_matched=False,
                verify_note=r"Read error from C:\Users\Jane Doe\file.log",
                expected_value=1000,
                observed_value=None,
            )
            result = SimpleNamespace(
                label=RestoreResultLabel.RESTORE_FAILED,
                attempted=1,
                wrote_succeeded=0,
                write_failed=1,
                verified_matched=0,
                could_not_verify=0,
                mismatched=0,
                before_restore_point_id=None,
                completed_at="2026-11-25T00:00:00Z",
                sensitivity_downgrades=[],
                unverified_writes=[],
                fields=[outcome],
            )
            state = restore_points.RestorePointsScreenState(result=result)
            shell = SimpleNamespace(
                restore_points_screen_state=state,
                restore_points_export_dir_override=export_dir,
                rebuild_current_screen=mock.MagicMock(),
            )
            restore_points._on_save_result_to_diagnostics(shell)
            written = list(export_dir.glob("restore_result_*.json"))
            self.assertEqual(len(written), 1)
            raw = written[0].read_text(encoding="utf-8")
            self.assertNotIn("Jane Doe", raw)
            self.assertNotIn("12345678", raw)
            parsed = json.loads(raw)
            self.assertEqual(parsed["kind"], "restore_result")


if __name__ == "__main__":
    unittest.main()
