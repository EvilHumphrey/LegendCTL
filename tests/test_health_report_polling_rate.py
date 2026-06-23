"""Regression test for the Health Report polling-rate wiring.

The bug fixed here: ``_build_default_health_report_service`` previously read
``rate.value`` (the selector byte 1..6) into ``DeviceContext.configured_polling_hz``,
which then leaked into the preface tile, Markdown device table, JSON export, and
the cadence ``expected_interval_ms = 1000 / configured_polling_hz`` calculation.
The fix routes ``PollingRate`` through ``POLLING_RATE_HZ`` so the field really
carries Hz.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from zd_app.services.health_report import DeviceContext
from zd_app.services.settings_service import POLLING_RATE_HZ, PollingRate
from zd_app.ui.app_shell import _build_default_health_report_service


class _FakeDeviceService:
    def __init__(self, product_name: str | None = "ZD Ultimate Legend") -> None:
        self.state = SimpleNamespace(product_name=product_name)


class _FakeProfileStore:
    def list_profiles(self):
        return ([], None)


def _device_context_from_rate(rate: PollingRate | None) -> DeviceContext:
    settings_service = MagicMock()
    settings_service.get_polling_rate.return_value = rate

    service = _build_default_health_report_service(
        settings_service=settings_service,
        device_service=_FakeDeviceService(),
        wrapper_profile_store=_FakeProfileStore(),
    )
    return service._device_context_provider()


class HealthReportPollingRateWiringTests(unittest.TestCase):
    def test_every_polling_rate_member_lands_as_integer_hz(self) -> None:
        for rate, expected_hz in POLLING_RATE_HZ.items():
            with self.subTest(rate=rate):
                ctx = _device_context_from_rate(rate)
                self.assertEqual(ctx.configured_polling_hz, expected_hz)
                self.assertNotEqual(
                    ctx.configured_polling_hz,
                    rate.value,
                    f"selector byte {rate.value} leaked into Hz field for {rate}",
                )

    def test_8000hz_specifically_is_8000_not_6(self) -> None:
        ctx = _device_context_from_rate(PollingRate.HZ_8000)

        self.assertEqual(ctx.configured_polling_hz, 8000)

    def test_none_polling_rate_yields_none_hz(self) -> None:
        ctx = _device_context_from_rate(None)

        self.assertIsNone(ctx.configured_polling_hz)

    def test_settings_service_lookup_raises_yields_none_no_crash(self) -> None:
        settings_service = MagicMock()
        settings_service.get_polling_rate.side_effect = RuntimeError("device fault")

        service = _build_default_health_report_service(
            settings_service=settings_service,
            device_service=_FakeDeviceService(),
            wrapper_profile_store=_FakeProfileStore(),
        )
        ctx = service._device_context_provider()

        self.assertIsNone(ctx.configured_polling_hz)

    def test_settings_service_absent_yields_none_hz(self) -> None:
        service = _build_default_health_report_service(
            settings_service=None,
            device_service=_FakeDeviceService(),
            wrapper_profile_store=_FakeProfileStore(),
        )
        ctx = service._device_context_provider()

        self.assertIsNone(ctx.configured_polling_hz)


if __name__ == "__main__":
    unittest.main()
