"""Tests for read-only HID model fingerprint collection."""

from __future__ import annotations

import ctypes as c
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from zd_app import i18n
from zd_app.services import model_fingerprint
from zd_app.services.model_fingerprint import (
    HIDP_STATUS_SUCCESS,
    InterfaceInventory,
    ModelFingerprint,
    canonical_rendering,
    collect_model_fingerprint,
    interface_inventory_from_entries,
)


_DEVICE_PATH = r"\\?\hid#vid_413d&pid_2104&mi_02#7&demo&0&0000#{guid}"
_SECOND_DEVICE_PATH = r"\\?\hid#vid_413d&pid_2104&mi_02#8&demo&0&0000#{guid}"
_LOCALE_DIR = Path("zd_app/i18n/locales")


class _StrictKernel32:
    def __init__(self, *, handle=0x1234) -> None:
        self.handle = handle
        self.create_calls = []
        self.closed_handles = []

    def CreateFileW(
        self,
        path,
        desired_access,
        share_mode,
        _security_attributes,
        creation_disposition,
        flags_and_attributes,
        _template_file,
    ):
        self.create_calls.append(
            (
                path,
                desired_access,
                share_mode,
                creation_disposition,
                flags_and_attributes,
            )
        )
        expected = (
            _DEVICE_PATH,
            model_fingerprint.METADATA_OPEN_DESIRED_ACCESS,
            model_fingerprint.METADATA_OPEN_SHARE_MODE,
            model_fingerprint.OPEN_EXISTING,
            model_fingerprint.METADATA_OPEN_FLAGS_AND_ATTRIBUTES,
        )
        if self.create_calls[-1] != expected:
            return model_fingerprint.INVALID_HANDLE_VALUE
        return self.handle

    def CloseHandle(self, handle) -> bool:
        self.closed_handles.append(handle)
        return True

    def GetLastError(self) -> int:
        return 0


class _StrictHidsdi:
    def __init__(
        self,
        *,
        attributes_ok: bool = True,
        product_ok: bool = True,
        manufacturer_ok: bool = True,
        preparsed_ok: bool = True,
        product_value: str = "ZD Ultimate Legend",
        manufacturer_value: str = "ZD",
        expected_handle=None,
    ) -> None:
        self.attributes_ok = attributes_ok
        self.product_ok = product_ok
        self.manufacturer_ok = manufacturer_ok
        self.preparsed_ok = preparsed_ok
        self.product_value = product_value
        self.manufacturer_value = manufacturer_value
        self.expected_handle = expected_handle
        self.preparsed_handle = 0xBEEF
        self.attributes_size_seen = None
        self.product_buffer_lengths = []
        self.manufacturer_buffer_lengths = []
        self.freed_preparsed = []
        self.handles_seen = []
        self.serial_accessed = False

    def __getattr__(self, name: str):
        if name == "HidD_GetSerialNumberString":
            self.serial_accessed = True
        raise AttributeError(name)

    def _record_handle(self, handle) -> bool:
        self.handles_seen.append(handle)
        return self.expected_handle is None or handle == self.expected_handle

    def HidD_GetAttributes(self, handle, attributes_ptr) -> bool:
        if not self._record_handle(handle):
            return False
        attrs = attributes_ptr._obj
        self.attributes_size_seen = attrs.Size
        if not self.attributes_ok:
            return False
        if attrs.Size != c.sizeof(model_fingerprint._HIDD_ATTRIBUTES):
            return False
        attrs.VendorID = 0x413D
        attrs.ProductID = 0x2104
        attrs.VersionNumber = 0x0124
        return True

    def HidD_GetProductString(self, handle, buffer, buffer_length_bytes) -> bool:
        if not self._record_handle(handle):
            return False
        self.product_buffer_lengths.append(buffer_length_bytes)
        if not self.product_ok:
            return False
        buffer.value = self.product_value
        return True

    def HidD_GetManufacturerString(self, handle, buffer, buffer_length_bytes) -> bool:
        if not self._record_handle(handle):
            return False
        self.manufacturer_buffer_lengths.append(buffer_length_bytes)
        if not self.manufacturer_ok:
            return False
        buffer.value = self.manufacturer_value
        return True

    def HidD_GetPreparsedData(self, handle, preparsed_ptr) -> bool:
        if not self._record_handle(handle):
            return False
        if not self.preparsed_ok:
            return False
        preparsed_ptr._obj.value = self.preparsed_handle
        return True

    def HidD_FreePreparsedData(self, preparsed_data) -> bool:
        self.freed_preparsed.append(preparsed_data.value)
        return True


class _StrictHid:
    def __init__(
        self,
        *,
        caps_ok: bool = True,
        expected_preparsed: int = 0xBEEF,
        raise_on_caps: bool = False,
    ) -> None:
        self.caps_ok = caps_ok
        self.expected_preparsed = expected_preparsed
        self.raise_on_caps = raise_on_caps
        self.get_caps_calls = 0

    def HidP_GetCaps(self, preparsed_data, caps_ptr) -> int:
        self.get_caps_calls += 1
        if self.raise_on_caps:
            raise RuntimeError("caps boom")
        if preparsed_data.value != self.expected_preparsed:
            return -1
        if not self.caps_ok:
            return -1
        caps = caps_ptr._obj
        caps.UsagePage = 0xFF00
        caps.Usage = 0x0001
        caps.InputReportByteLength = 64
        caps.OutputReportByteLength = 65
        caps.FeatureReportByteLength = 17
        caps.NumberInputButtonCaps = 7
        caps.NumberOutputButtonCaps = 1
        caps.NumberFeatureButtonCaps = 2
        caps.NumberInputValueCaps = 4
        caps.NumberOutputValueCaps = 1
        caps.NumberFeatureValueCaps = 1
        return HIDP_STATUS_SUCCESS


class ModelFingerprintCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _collect(
        self,
        *,
        kernel32: _StrictKernel32 | None = None,
        hidsdi: _StrictHidsdi | None = None,
        hid: _StrictHid | None = None,
    ) -> tuple[ModelFingerprint, _StrictKernel32, _StrictHidsdi, _StrictHid]:
        kernel32 = kernel32 or _StrictKernel32()
        hidsdi = hidsdi or _StrictHidsdi()
        hid = hid or _StrictHid()
        entries = [
            {"instance_id": r"USB\VID_413D&PID_2104&MI_00\ROOT"},
            {"instance_id": r"HID\VID_413D&PID_2104&MI_02\UNIT"},
            {"instance_id": r"HID\VID_413D&PID_2104&MI_03\UNIT"},
        ]
        with patch.object(
            model_fingerprint._Win32,
            "kernel32",
            return_value=kernel32,
        ), patch.object(
            model_fingerprint._Win32,
            "hidsdi",
            return_value=hidsdi,
        ), patch.object(
            model_fingerprint._Win32,
            "hid",
            return_value=hid,
        ):
            fingerprint = collect_model_fingerprint(
                device_path=_DEVICE_PATH,
                pnp_entries=entries,
            )
        return fingerprint, kernel32, hidsdi, hid

    def test_collects_ul_like_shape_with_metadata_only_open_and_frees_preparsed_data(self) -> None:
        fingerprint, kernel32, hidsdi, hid = self._collect()

        self.assertEqual(fingerprint.vid, 0x413D)
        self.assertEqual(fingerprint.pid, 0x2104)
        self.assertEqual(fingerprint.version_number, 0x0124)
        self.assertEqual(fingerprint.product_string, "ZD Ultimate Legend")
        self.assertEqual(fingerprint.manufacturer_string, "ZD")
        self.assertEqual(fingerprint.usage_page, 0xFF00)
        self.assertEqual(fingerprint.usage, 0x0001)
        self.assertEqual(fingerprint.input_report_len, 64)
        self.assertEqual(fingerprint.output_report_len, 65)
        self.assertEqual(fingerprint.feature_report_len, 17)
        self.assertEqual(fingerprint.button_caps_count, 10)
        self.assertEqual(fingerprint.value_caps_count, 6)
        self.assertEqual(
            fingerprint.interface_inventory,
            InterfaceInventory(count=3, mi_indices=(0, 2, 3)),
        )
        self.assertEqual(len(fingerprint.digest or ""), 64)
        self.assertEqual(len(fingerprint.short_digest or ""), 12)
        self.assertEqual(hidsdi.attributes_size_seen, c.sizeof(model_fingerprint._HIDD_ATTRIBUTES))
        self.assertEqual(hidsdi.freed_preparsed, [hidsdi.preparsed_handle])
        self.assertEqual(hid.get_caps_calls, 1)
        self.assertEqual(kernel32.closed_handles, [kernel32.handle])
        self.assertFalse(hidsdi.serial_accessed)

    def test_ctypes_handle_values_are_not_truncated(self) -> None:
        high_handle = 0x1234567887654321
        kernel32 = _StrictKernel32(handle=high_handle)
        hidsdi = _StrictHidsdi(expected_handle=high_handle)

        fingerprint, kernel32, hidsdi, _hid = self._collect(
            kernel32=kernel32,
            hidsdi=hidsdi,
        )

        self.assertEqual(fingerprint.vid, 0x413D)
        self.assertTrue(hidsdi.handles_seen)
        self.assertTrue(all(handle == high_handle for handle in hidsdi.handles_seen))
        self.assertEqual(kernel32.closed_handles, [high_handle])

    def test_ctypes_invalid_handle_object_is_treated_as_failed(self) -> None:
        kernel32 = _StrictKernel32(handle=c.c_void_p(model_fingerprint.INVALID_HANDLE_VALUE))
        hidsdi = _StrictHidsdi()
        hid = _StrictHid()

        fingerprint, kernel32, hidsdi, hid = self._collect(
            kernel32=kernel32,
            hidsdi=hidsdi,
            hid=hid,
        )

        self.assertIsNone(fingerprint.vid)
        self.assertEqual(hidsdi.handles_seen, [])
        self.assertEqual(hid.get_caps_calls, 0)
        self.assertEqual(kernel32.closed_handles, [])

    def test_false_returns_degrade_fields_to_none_without_raising(self) -> None:
        hidsdi = _StrictHidsdi(
            attributes_ok=False,
            product_ok=False,
            manufacturer_ok=False,
            preparsed_ok=False,
        )
        fingerprint, _kernel32, hidsdi, hid = self._collect(hidsdi=hidsdi)

        self.assertIsNone(fingerprint.vid)
        self.assertIsNone(fingerprint.pid)
        self.assertIsNone(fingerprint.version_number)
        self.assertIsNone(fingerprint.product_string)
        self.assertIsNone(fingerprint.manufacturer_string)
        self.assertIsNone(fingerprint.usage_page)
        self.assertIsNone(fingerprint.usage)
        self.assertIsNone(fingerprint.input_report_len)
        self.assertIsNone(fingerprint.output_report_len)
        self.assertIsNone(fingerprint.feature_report_len)
        self.assertIsNone(fingerprint.button_caps_count)
        self.assertIsNone(fingerprint.value_caps_count)
        self.assertEqual(
            fingerprint.interface_inventory,
            InterfaceInventory(count=3, mi_indices=(0, 2, 3)),
        )
        self.assertEqual(hidsdi.freed_preparsed, [])
        self.assertEqual(hid.get_caps_calls, 0)

    def test_get_caps_failure_still_frees_preparsed_data(self) -> None:
        hidsdi = _StrictHidsdi()
        hid = _StrictHid(caps_ok=False)

        fingerprint, _kernel32, hidsdi, hid = self._collect(hidsdi=hidsdi, hid=hid)

        self.assertIsNone(fingerprint.usage_page)
        self.assertIsNone(fingerprint.input_report_len)
        self.assertEqual(hidsdi.freed_preparsed, [hidsdi.preparsed_handle])
        self.assertEqual(hid.get_caps_calls, 1)

    def test_get_caps_exception_still_frees_preparsed_data(self) -> None:
        hidsdi = _StrictHidsdi()
        hid = _StrictHid(raise_on_caps=True)

        fingerprint, _kernel32, hidsdi, hid = self._collect(hidsdi=hidsdi, hid=hid)

        self.assertIsNone(fingerprint.usage_page)
        self.assertEqual(hidsdi.freed_preparsed, [hidsdi.preparsed_handle])
        self.assertEqual(hid.get_caps_calls, 1)

    def test_hidsdi_loader_failure_closes_open_handle(self) -> None:
        kernel32 = _StrictKernel32()
        with patch.object(
            model_fingerprint._Win32,
            "kernel32",
            return_value=kernel32,
        ), patch.object(
            model_fingerprint._Win32,
            "hidsdi",
            side_effect=RuntimeError("hid dll bind failed"),
        ), patch.object(
            model_fingerprint._Win32,
            "hid",
            side_effect=AssertionError("hid should not bind after hidsdi failure"),
        ):
            fingerprint = collect_model_fingerprint(device_path=_DEVICE_PATH)

        self.assertIsNone(fingerprint.vid)
        self.assertEqual(kernel32.closed_handles, [kernel32.handle])

    def test_multiple_default_mi02_paths_abstain_instead_of_guessing(self) -> None:
        kernel32 = _StrictKernel32()
        hidsdi = _StrictHidsdi()
        hid = _StrictHid()
        entries = [{"instance_id": r"HID\VID_413D&PID_2104&MI_02\UNIT"}]

        with patch(
            "zd_app.services.settings_service.enumerate_mi02_hid_paths",
            return_value=[_DEVICE_PATH, _SECOND_DEVICE_PATH],
        ), patch.object(
            model_fingerprint._Win32,
            "kernel32",
            return_value=kernel32,
        ), patch.object(
            model_fingerprint._Win32,
            "hidsdi",
            return_value=hidsdi,
        ), patch.object(
            model_fingerprint._Win32,
            "hid",
            return_value=hid,
        ):
            fingerprint = collect_model_fingerprint(pnp_entries=entries)

        self.assertEqual(kernel32.create_calls, [])
        self.assertEqual(hidsdi.handles_seen, [])
        self.assertEqual(hid.get_caps_calls, 0)
        self.assertIsNone(fingerprint.vid)
        self.assertEqual(
            fingerprint.interface_inventory,
            InterfaceInventory(count=1, mi_indices=(2,)),
        )

    def test_interface_inventory_uses_sorted_unique_mi_indices(self) -> None:
        inventory = interface_inventory_from_entries(
            [
                {"instance_id": r"USB\VID_413D&PID_2104&MI_02\A"},
                {"instance_id": r"HID\VID_413D&PID_2104&MI_00\B"},
                {"path": r"\\?\hid#vid_413d&pid_2104&mi_02#B"},
            ]
        )

        self.assertEqual(inventory, InterfaceInventory(count=2, mi_indices=(0, 2)))

    def test_digest_is_stable_and_distinguishes_ul_like_from_sle_like_shape(self) -> None:
        ul_like = _ul_like_fingerprint()
        ul_like_again = _ul_like_fingerprint()
        sle_like = ModelFingerprint(
            vid=0x413D,
            pid=0x2104,
            version_number=0x0201,
            product_string="Super Legend Excellence",
            manufacturer_string="ZD",
            usage_page=0xFF00,
            usage=0x0001,
            input_report_len=64,
            output_report_len=65,
            feature_report_len=33,
            button_caps_count=12,
            value_caps_count=8,
            interface_inventory=InterfaceInventory(count=2, mi_indices=(0, 2)),
        )

        self.assertEqual(ul_like.digest, ul_like_again.digest)
        self.assertEqual(canonical_rendering(ul_like), canonical_rendering(ul_like_again))
        self.assertNotEqual(ul_like.digest, sle_like.digest)


@unittest.skipUnless(sys.platform == "win32", "real Win32 DLL binding")
class ModelFingerprintRealDllBindingTests(unittest.TestCase):
    """Bind the REAL DLLs (no device I/O) so a wrong library name fails here.

    The fakes above patch the _Win32 classmethods, so they can never catch a
    bad WinDLL name — WinDLL("hidsdi") shipped green through the whole suite
    and blanked every HidD field on real hardware (2026-07-03 operator smoke).
    """

    def test_hidd_functions_bind_from_real_dll(self) -> None:
        hidsdi = model_fingerprint._Win32.hidsdi()
        for name in (
            "HidD_GetAttributes",
            "HidD_GetProductString",
            "HidD_GetManufacturerString",
            "HidD_GetPreparsedData",
            "HidD_FreePreparsedData",
        ):
            self.assertTrue(callable(getattr(hidsdi, name)))

    def test_hidp_getcaps_binds_from_real_dll(self) -> None:
        self.assertTrue(callable(model_fingerprint._Win32.hid().HidP_GetCaps))

    def test_hidd_restypes_are_single_byte_boolean(self) -> None:
        # H fix: HidD_* return Windows BOOLEAN (1 byte), not BOOL (4 bytes) — a
        # BOOL binding would read 3 uninitialized upper return-register bytes and
        # could misread a TRUE as FALSE. Lock the restype width so it can't
        # regress to BOOL.
        hidsdi = model_fingerprint._Win32.hidsdi()
        for name in (
            "HidD_GetAttributes",
            "HidD_GetProductString",
            "HidD_GetManufacturerString",
            "HidD_GetPreparsedData",
            "HidD_FreePreparsedData",
        ):
            with self.subTest(func=name):
                self.assertEqual(c.sizeof(getattr(hidsdi, name).restype), 1)
        # HidP_GetCaps stays a 4-byte NTSTATUS (must NOT become a 1-byte BOOLEAN).
        self.assertEqual(
            c.sizeof(model_fingerprint._Win32.hid().HidP_GetCaps.restype), 4
        )

    def test_hidp_caps_and_attributes_struct_marshalling(self) -> None:
        # G2: lock the exact struct layout whose marshalling blindness let the
        # hidsdi binding bug ship green. Pure ctypes layout — no device I/O.
        caps = model_fingerprint._HIDP_CAPS
        self.assertEqual(c.sizeof(caps), 64)
        self.assertEqual(caps.Usage.offset, 0)
        self.assertEqual(caps.UsagePage.offset, 2)
        self.assertEqual(caps.NumberInputButtonCaps.offset, 46)
        self.assertEqual(c.sizeof(model_fingerprint._HIDD_ATTRIBUTES), 12)


class ModelFingerprintI18nTests(unittest.TestCase):
    def test_en_zh_cn_have_matching_model_fingerprint_keys(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))

        en_keys = {key for key in en if key.startswith("model_fingerprint.")}
        zh_keys = {key for key in zh if key.startswith("model_fingerprint.")}

        self.assertEqual(en_keys, zh_keys)
        self.assertGreaterEqual(len(en_keys), 19)

    def test_model_fingerprint_locale_placeholders_match(self) -> None:
        en = json.loads((_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
        zh = json.loads((_LOCALE_DIR / "zh-CN.json").read_text(encoding="utf-8"))
        keys = [
            *(key for key in en if key.startswith("model_fingerprint.")),
            "log.model_fingerprint.collected",
        ]

        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(set(_placeholders(en[key])), set(_placeholders(zh[key])))


def _ul_like_fingerprint() -> ModelFingerprint:
    return ModelFingerprint(
        vid=0x413D,
        pid=0x2104,
        version_number=0x0124,
        product_string="ZD Ultimate Legend",
        manufacturer_string="ZD",
        usage_page=0xFF00,
        usage=0x0001,
        input_report_len=64,
        output_report_len=65,
        feature_report_len=17,
        button_caps_count=10,
        value_caps_count=6,
        interface_inventory=InterfaceInventory(count=3, mi_indices=(0, 1, 2)),
    )


def _placeholders(value: str) -> list[str]:
    parts = []
    cursor = 0
    while True:
        start = value.find("{", cursor)
        if start == -1:
            return parts
        end = value.find("}", start)
        if end == -1:
            return parts
        parts.append(value[start + 1:end])
        cursor = end + 1


if __name__ == "__main__":
    unittest.main()
