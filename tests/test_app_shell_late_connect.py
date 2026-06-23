"""Late-connect wiring tests for ``AppShell.attach_settings_service``.

When the wrapper boots with no controller, ``main_zd`` constructs AppShell
with ``settings_service=None``: the shell-owned SettingsApplyCoordinator is
bound to None and ``restore_point_service`` stays None. The watchdog used to
flip only ``shell.settings_service`` on late connect, which left footer
Apply-profile / Safe-Import apply permanently failing and the Restore Points
screen "unavailable" until restart.

``attach_settings_service`` is the single late-bind entry point: it rebinds
the coordinator, builds the missing RestorePointService against the same
coordinator instance, and requests hydration. These tests pin that contract
headlessly (no dpg context), mirroring the stub construction patterns in
``tests/test_app_shell_copy.py`` / ``tests/r2_shell_test_helpers.py``.
"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.r2_shell_test_helpers import empty_snapshot
from zd_app.models import AppSettings, DeviceState
from zd_app.services.settings_service import PollingRate, SetPollingRateOutcome
from zd_app.storage.restore_point_store import RestorePointStore
from zd_app.ui.app_shell import AppShell


class _StubSettingsService:
    """Write-capable stub covering only what a polling-rate apply touches."""

    def __init__(self) -> None:
        self.polling_writes: list[PollingRate] = []

    def set_polling_rate(self, rate: PollingRate):
        self.polling_writes.append(rate)
        return SimpleNamespace(outcome=SetPollingRateOutcome.OK)


def _make_shell(*, settings_service=None, restore_point_service=None) -> AppShell:
    """Headless AppShell with stubbed services.

    Mirrors ``r2_shell_test_helpers.make_shell`` EXCEPT that
    ``restore_point_service`` passes through unmodified — these tests need
    the production ``settings_service=None -> restore_point_service=None``
    construction state the late-connect attach path starts from.
    """

    settings_store = MagicMock()
    settings_store.load.return_value = AppSettings()
    device_service = MagicMock()
    device_service.state = DeviceState()
    device_service.recent_events.return_value = []
    device_service.summary_source_summary.return_value = "Not verified"
    device_service.last_read_duration_ms = None
    device_service.last_write_duration_ms = None
    profile_service = MagicMock()
    profile_service.pending_changes_count.return_value = 0
    diagnostics_service = MagicMock()
    wrapper_store = MagicMock()
    wrapper_store.list_profiles.return_value = []
    return AppShell(
        device_service=device_service,
        profile_service=profile_service,
        diagnostics_service=diagnostics_service,
        settings_store=settings_store,
        preflight_service=MagicMock(),
        settings_service=settings_service,
        wrapper_profile_store=wrapper_store,
        restore_point_service=restore_point_service,
    )


def _tempdir_rp_store_patch(tmpdir: str):
    """Redirect the default-builder's ``RestorePointStore()`` to ``tmpdir``.

    ``_build_default_restore_point_service`` constructs a no-arg store, which
    would otherwise mkdir + log against the real ``<user_data_dir>``.
    """

    return patch(
        "zd_app.ui.app_shell.RestorePointStore",
        lambda: RestorePointStore(tmpdir),
    )


class AttachSettingsServiceTests(unittest.TestCase):
    # Regression for the late-connect bind: before attach_settings_service
    # existed, the old watchdog path (flip shell.settings_service + request
    # hydration) left the coordinator bound to None, so every profile apply
    # failed with a 'settings_service' row.
    def test_attach_revives_profile_apply_after_late_connect(self) -> None:
        shell = _make_shell()
        self.assertIsNone(shell.settings_service)
        self.assertIsNone(shell.restore_point_service)
        stub = _StubSettingsService()

        with tempfile.TemporaryDirectory() as tmp, _tempdir_rp_store_patch(tmp):
            shell.attach_settings_service(stub)

        self.assertTrue(shell._needs_hydration)
        result = shell._apply_snapshot_to_controller(
            empty_snapshot(polling_rate=PollingRate.HZ_8000)
        )
        self.assertEqual(result.succeeded, 1)
        self.assertNotIn(
            "settings_service",
            [failure.setting_label for failure in result.failed],
        )
        self.assertEqual(result.failed, [])
        self.assertEqual(stub.polling_writes, [PollingRate.HZ_8000])

    # T2
    def test_attach_builds_restore_point_service_on_shared_coordinator(self) -> None:
        shell = _make_shell()
        stub = _StubSettingsService()

        with tempfile.TemporaryDirectory() as tmp, _tempdir_rp_store_patch(tmp):
            shell.attach_settings_service(stub)
            self.assertIs(shell.settings_service, stub)
            self.assertIsNotNone(shell.restore_point_service)
            self.assertIs(
                shell.restore_point_service._apply_coordinator,
                shell._apply_coordinator,
            )

    # T3
    def test_attach_is_idempotent(self) -> None:
        shell = _make_shell()
        stub = _StubSettingsService()

        with tempfile.TemporaryDirectory() as tmp, _tempdir_rp_store_patch(tmp):
            shell.attach_settings_service(stub)
            first_service = shell.restore_point_service
            shell.attach_settings_service(stub)  # second call must be safe
            self.assertIs(shell.restore_point_service, first_service)
            self.assertIs(shell.settings_service, stub)

    # Construction-time binding is unchanged by the late-connect work.
    def test_construction_with_service_binds_coordinator_directly(self) -> None:
        stub = _StubSettingsService()
        shell = _make_shell(
            settings_service=stub, restore_point_service=MagicMock()
        )
        self.assertIs(shell._apply_coordinator._settings_service, stub)


if __name__ == "__main__":
    unittest.main()
