from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zd_app.protocol import hid_transport as hht


class HiddenTransportTests(unittest.TestCase):
    def test_official_open_profiles_match_v3_capture(self) -> None:
        self.assertEqual(hht.PUBLIC_IDENTIFY_OPEN_PROFILE.desired_access, hht.GENERIC_READ | hht.GENERIC_WRITE)
        self.assertEqual(hht.PUBLIC_IDENTIFY_OPEN_PROFILE.share_mode, hht.FILE_SHARE_READ | hht.FILE_SHARE_WRITE)
        self.assertEqual(hht.PUBLIC_IDENTIFY_OPEN_PROFILE.creation_disposition, hht.OPEN_EXISTING)
        self.assertEqual(hht.PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes, hht.FILE_ATTRIBUTE_NORMAL)

        self.assertEqual(hht.HIDDEN_BOOTSTRAP_OPEN_PROFILE.desired_access, hht.GENERIC_WRITE)
        self.assertEqual(hht.HIDDEN_BOOTSTRAP_OPEN_PROFILE.flags_and_attributes, 0)

        self.assertEqual(hht.HIDDEN_READBACK_OPEN_PROFILE.desired_access, hht.GENERIC_READ | hht.GENERIC_WRITE)
        self.assertEqual(hht.HIDDEN_READBACK_OPEN_PROFILE.flags_and_attributes, hht.FILE_FLAG_OVERLAPPED)

    def test_known_hidden_paths_from_logs_extracts_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sample_path = r"\\?\hid#vid_20bc&pid_5080#7&8b7d2c7&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
            payloads = [
                {"path": sample_path, "type": "createfile"},
                {"path": sample_path, "type": "handle-open"},
                {"path": r"\\?\usb#vid_413d&pid_2104&mi_00#x", "type": "createfile"},
            ]
            log_path = Path(temp_dir) / "capture.jsonl"
            log_path.write_text("\n".join(json.dumps(item) for item in payloads), encoding="utf-8")

            matches = hht.known_hidden_paths_from_logs(temp_dir, vendor_id=0x20BC, product_id=0x5080)

            self.assertEqual(matches, [sample_path])

    def test_wait_for_openable_hid_paths_uses_log_cache_when_enumeration_is_empty(self) -> None:
        original_filter = hht.filter_hid_paths
        original_known = hht.known_hidden_paths_from_logs
        original_try_open = hht.try_open_hid_path
        try:
            sample_path = r"\\?\hid#vid_20bc&pid_5080#7&8b7d2c7&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
            hht.filter_hid_paths = lambda vendor_id=None, product_id=None: []
            hht.known_hidden_paths_from_logs = lambda log_dir="logs", vendor_id=None, product_id=None: [sample_path]
            hht.try_open_hid_path = lambda path, desired_access: {"ok": path == sample_path, "last_error": 0}

            matches = hht.wait_for_openable_hid_paths(0x20BC, 0x5080, timeout_s=0.0)

            self.assertEqual(matches, [sample_path])
        finally:
            hht.filter_hid_paths = original_filter
            hht.known_hidden_paths_from_logs = original_known
            hht.try_open_hid_path = original_try_open

    def test_probe_hidden_transport_distinguishes_visible_known_and_reachable(self) -> None:
        original_filter = hht.filter_hid_paths
        original_known = hht.known_hidden_paths_from_logs
        original_try_open = hht.try_open_hid_path
        try:
            visible_path = r"\\?\hid#vid_20bc&pid_5080#visible"
            cached_path = r"\\?\hid#vid_20bc&pid_5080#cached"
            shared_path = r"\\?\hid#vid_20bc&pid_5080#shared"
            hht.filter_hid_paths = lambda vendor_id=None, product_id=None: [visible_path, shared_path]
            hht.known_hidden_paths_from_logs = lambda log_dir="logs", vendor_id=None, product_id=None: [cached_path, shared_path]
            hht.try_open_hid_path = lambda path, desired_access: {"ok": path == shared_path, "last_error": 0}

            probe = hht.probe_hidden_transport(0x20BC, 0x5080)

            self.assertEqual(probe.visible_paths, (visible_path, shared_path))
            self.assertEqual(probe.known_candidate_paths, (cached_path, shared_path))
            self.assertEqual(probe.reachable_paths, (shared_path,))
            self.assertEqual(probe.source_for_path(visible_path), "setupapi")
            self.assertEqual(probe.source_for_path(cached_path), "log_cache")
            self.assertEqual(probe.source_for_path(shared_path), "setupapi+log_cache")
        finally:
            hht.filter_hid_paths = original_filter
            hht.known_hidden_paths_from_logs = original_known
            hht.try_open_hid_path = original_try_open


if __name__ == "__main__":
    unittest.main()
