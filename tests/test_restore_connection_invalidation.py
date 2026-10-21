"""Shell invalidation must discard a confirmation, not silently renew it."""

import unittest
from unittest.mock import MagicMock

from tests.r2_shell_test_helpers import make_shell
from zd_app.ui.screens import restore_points


class RestoreConnectionInvalidationTests(unittest.TestCase):
    def test_presence_invalidation_discards_prepared_confirmation(self):
        shell = make_shell(settings_service=MagicMock())
        shell._sync_profile_apply_readback_details_action = MagicMock()
        state = restore_points.RestorePointsScreenState()
        state.view = restore_points.VIEW_CONFIRM
        state.prepared_restore = object()
        state.write_only_paddles_acknowledged = True
        state.identity_unknown_acknowledged = True
        shell.restore_points_screen_state = state

        shell._invalidate_cached_controller_settings()

        self.assertIsNone(state.prepared_restore)
        self.assertFalse(state.write_only_paddles_acknowledged)
        self.assertFalse(state.identity_unknown_acknowledged)
        self.assertEqual(state.view, restore_points.VIEW_DETAIL)
        self.assertEqual(state.status_kind, "warn")

    def test_no_confirmation_is_created_by_presence_invalidation(self):
        shell = make_shell(settings_service=MagicMock())
        shell._sync_profile_apply_readback_details_action = MagicMock()
        self.assertIsNone(shell.restore_points_screen_state)
        shell._invalidate_cached_controller_settings()
        self.assertIsNone(shell.restore_points_screen_state)

    def test_explicit_read_invalidates_old_ui_before_readmitting(self):
        service = MagicMock()
        service.connection_requires_readmission = True
        shell = make_shell(settings_service=service)
        shell._hid_available_or_refuse = MagicMock(return_value=True)
        shell._sync_profile_apply_readback_details_action = MagicMock()
        shell.refresh_from_controller = MagicMock()
        shell._request_presence_rebuild = MagicMock()
        state = restore_points.RestorePointsScreenState()
        state.view = restore_points.VIEW_CONFIRM
        state.prepared_restore = object()
        state.identity_unknown_acknowledged = True
        shell.restore_points_screen_state = state
        generation = shell._controller_presence_generation

        def on_stop():
            self.assertIsNone(state.prepared_restore)
            self.assertFalse(state.identity_unknown_acknowledged)
            self.assertGreater(shell._controller_presence_generation, generation)
            shell.refresh_from_controller.assert_not_called()

        service.stop.side_effect = on_stop
        shell.read_current_connection()
        service.stop.assert_called_once()
        shell.refresh_from_controller.assert_called_once_with()
        shell._request_presence_rebuild.assert_called_once_with()

    def test_busy_read_does_not_clear_latch(self):
        service = MagicMock()
        service.connection_requires_readmission = True
        shell = make_shell(settings_service=service)
        shell._hid_available_or_refuse = MagicMock(return_value=False)
        shell.refresh_from_controller = MagicMock()
        shell.read_current_connection()
        service.stop.assert_not_called()
        shell.refresh_from_controller.assert_not_called()

    def test_ordinary_explicit_read_keeps_current_session(self):
        service = MagicMock()
        service.connection_requires_readmission = False
        shell = make_shell(settings_service=service)
        shell._hid_available_or_refuse = MagicMock(return_value=True)
        shell.refresh_from_controller = MagicMock()
        generation = shell._controller_presence_generation
        shell.read_current_connection()
        self.assertEqual(shell._controller_presence_generation, generation)
        service.stop.assert_not_called()
        shell.refresh_from_controller.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
