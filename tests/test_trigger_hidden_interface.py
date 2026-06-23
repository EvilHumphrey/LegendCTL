from __future__ import annotations

import unittest
from unittest import mock

import zd_app.protocol.trigger_interface as trigger_hidden_interface
from zd_app.protocol.trigger_interface import (
    HiddenOpenResult,
    HiddenReadObservation,
    PublicProbeResult,
    PUBLIC_IDENTIFY_EXPECTED_HEX,
    ordered_public_paths,
    public_open_attempts,
    parse_args,
    preferred_public_path,
    run_public_identify_probe,
    run_public_identify_probe_candidates,
    summarize_experiment,
)


class TriggerHiddenInterfaceTests(unittest.TestCase):
    def test_preferred_public_path_prefers_mi_00(self) -> None:
        paths = [
            r"\\?\hid#vid_413d&pid_2104&ig_00#c",
            r"\\?\usb#vid_413d&pid_2104&mi_01#b",
            r"\\?\usb#vid_413d&pid_2104&mi_00#a",
        ]

        chosen = preferred_public_path(paths)

        self.assertEqual(chosen, paths[2])

    def test_ordered_public_paths_prefers_usb_mi_00_over_generic_hid(self) -> None:
        paths = [
            r"\\?\HID#VID_413D&PID_2104&IG_00#9&1068e888&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}",
            r"\\?\usb#vid_413d&pid_2104&mi_00#7&1525ee77&1&0000#{ec87f1e3-c13b-4100-b5f7-8b84d54260cb}",
        ]

        ordered = ordered_public_paths(paths)

        self.assertEqual(ordered[0], paths[1])

    def test_public_open_attempts_auto_starts_with_metadata_only(self) -> None:
        attempts = public_open_attempts("auto")

        self.assertEqual(attempts[0][0], "metadata_only")
        self.assertEqual(attempts[1][0], "read_write")

    def test_parse_args_defaults_match_official_public_shape(self) -> None:
        args = parse_args([])

        self.assertEqual(args.public_open_mode, "read_write")
        self.assertEqual(args.share_mode, trigger_hidden_interface.FILE_SHARE_READ | trigger_hidden_interface.FILE_SHARE_WRITE)
        self.assertEqual(args.creation_disposition, trigger_hidden_interface.OPEN_EXISTING)
        self.assertEqual(args.flags_and_attributes, trigger_hidden_interface.PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes)

    def test_run_public_identify_probe_auto_retries_after_ioctl_failure(self) -> None:
        create_file_attempts: list[int] = []

        def fake_create_file(*args):
            create_file_attempts.append(args[1])
            return 1234

        def fake_device_io_control(handle, ioctl, in_buffer, in_len, out_buffer, out_len, bytes_returned, overlapped):
            if len(create_file_attempts) == 1:
                return 0
            out_buffer.raw = bytes.fromhex(PUBLIC_IDENTIFY_EXPECTED_HEX)
            return 1

        with mock.patch.object(trigger_hidden_interface, "CreateFileW", side_effect=fake_create_file), mock.patch.object(
            trigger_hidden_interface, "DeviceIoControl", side_effect=fake_device_io_control
        ), mock.patch.object(
            trigger_hidden_interface, "CloseHandle", return_value=True
        ), mock.patch.object(
            trigger_hidden_interface.kernel32, "GetLastError", return_value=5
        ):
            result = run_public_identify_probe(
                r"\\?\usb#vid_413d&pid_2104&mi_00#test",
                mode_name="auto",
                share_mode=trigger_hidden_interface.FILE_SHARE_READ | trigger_hidden_interface.FILE_SHARE_WRITE,
                creation_disposition=trigger_hidden_interface.OPEN_EXISTING,
                flags_and_attributes=0,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.access_mode, "read_write")
        self.assertTrue(result.matched_expected)
        self.assertEqual(create_file_attempts[:2], [0, trigger_hidden_interface.GENERIC_READ | trigger_hidden_interface.GENERIC_WRITE])

    def test_run_public_identify_probe_candidates_uses_preferred_path(self) -> None:
        candidates = [
            r"\\?\hid#vid_413d&pid_2104&ig_00#c",
            r"\\?\usb#vid_413d&pid_2104&mi_00#a",
            r"\\?\usb#vid_413d&pid_2104&mi_01#b",
        ]
        expected_result = PublicProbeResult(
            path=candidates[1],
            attempted=True,
            ok=True,
            last_error=0,
            access_mode="read_write",
            out_hex=PUBLIC_IDENTIFY_EXPECTED_HEX,
            matched_expected=True,
        )

        with mock.patch.object(trigger_hidden_interface, "run_public_identify_probe", return_value=expected_result) as probe:
            result = run_public_identify_probe_candidates(
                candidates,
                mode_name="auto",
                share_mode=trigger_hidden_interface.FILE_SHARE_READ | trigger_hidden_interface.FILE_SHARE_WRITE,
                creation_disposition=trigger_hidden_interface.OPEN_EXISTING,
                flags_and_attributes=0,
            )

        self.assertEqual(result, expected_result)
        probe.assert_called_once_with(
            candidates[1],
            mode_name="auto",
            share_mode=trigger_hidden_interface.FILE_SHARE_READ | trigger_hidden_interface.FILE_SHARE_WRITE,
            creation_disposition=trigger_hidden_interface.OPEN_EXISTING,
            flags_and_attributes=0,
        )

    def test_summarize_experiment_marks_reachable_on_rw_open_without_baseline(self) -> None:
        public_probe = PublicProbeResult(
            path="public",
            attempted=True,
            ok=True,
            last_error=0,
            access_mode="read_write",
            out_hex="03010100000000003d410421",
            matched_expected=True,
        )
        hidden_open = HiddenOpenResult(
            path="hidden",
            visible=True,
            open_ok=True,
            last_error=0,
            visible_after_ms=12,
        )
        hidden_read = HiddenReadObservation(
            opened_with_hidapi=True,
            observed_frame_count=0,
            observed_frames=(),
            ready=False,
            message="No unsolicited baseline traffic was observed within the read window.",
        )

        result = summarize_experiment(
            send_attempted=True,
            public_candidates=["public"],
            public_probe=public_probe,
            hidden_visible_paths=["hidden"],
            hidden_open=hidden_open,
            hidden_read=hidden_read,
        )

        self.assertEqual(result.transport_state, "Reachable (Read/Write Open)")
        self.assertEqual(result.state_ladder, "candidate -> reachable")

    def test_summarize_experiment_marks_ready_when_unsolicited_baseline_is_seen(self) -> None:
        public_probe = PublicProbeResult(
            path="public",
            attempted=True,
            ok=True,
            last_error=0,
            access_mode="read_write",
            out_hex="03010100000000003d410421",
            matched_expected=True,
        )
        hidden_open = HiddenOpenResult(
            path="hidden",
            visible=True,
            open_ok=True,
            last_error=0,
            visible_after_ms=12,
        )
        hidden_read = HiddenReadObservation(
            opened_with_hidapi=True,
            observed_frame_count=1,
            observed_frames=("3055aad00f000300",),
            ready=True,
            message="Observed unsolicited baseline traffic on the hidden path.",
        )

        result = summarize_experiment(
            send_attempted=True,
            public_candidates=["public"],
            public_probe=public_probe,
            hidden_visible_paths=["hidden"],
            hidden_open=hidden_open,
            hidden_read=hidden_read,
        )

        self.assertEqual(result.transport_state, "Ready (Unsolicited Baseline Observed)")
        self.assertEqual(result.state_ladder, "candidate -> reachable -> ready")


if __name__ == "__main__":
    unittest.main()
