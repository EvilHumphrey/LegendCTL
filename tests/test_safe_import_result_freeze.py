"""The Safe Import success opener must describe the import it was built for.

``_commit_safe_import_apply_result`` splits committed bookkeeping from the
optional modal: the bookkeeping runs immediately, the modal rides the deferred
swap and opens two frames later. The failure branch closed over its own
``apply_result``, but the success branch returned
``lambda: safe_import.open_result(self)`` and ``open_result`` re-reads the
mutable ``self._safe_import_result`` when it finally runs. Anything that
replaced the result inside that window would have rendered the *later* import
under this operation's confirmation.

No ordinary user path performs that replacement in the two-frame window today,
so this is defensive. It is still worth pinning: the docstring on the committer
promises that the result the modal describes is decided at commit time, and a
promise the code does not keep is the shape this project keeps having to fix.
"""

from __future__ import annotations

import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from zd_app.ui import app_shell as app_shell_module
from zd_app.ui.app_shell import AppShell


def _fake_shell(result):
    """The smallest duck for the committer: no viewport, no device, no store."""

    shell = SimpleNamespace(
        _safe_import_result=result,
        _last_apply_result=None,
        rebuild_current_screen=lambda: None,
        _record_settings_apply_result=lambda ok, message: None,
        _refresh_footer_profile_combo=lambda selected_name=None: None,
        _sync_profile_apply_readback_details_action=lambda: None,
        _with_sensitivity_downgrade_notice=lambda message, applied: message,
        _show_apply_failure_modal=lambda applied: None,
    )
    shell._open_frozen_safe_import_result = types.MethodType(
        AppShell._open_frozen_safe_import_result, shell
    )
    return shell


class TheSafeImportResultOpenerFreezesItsResultTests(unittest.TestCase):
    def _opener(self, shell):
        return AppShell._commit_safe_import_apply_result(
            shell, "profile", apply_to_controller=False
        )

    def test_the_opener_renders_the_result_committed_with_it(self) -> None:
        committed = SimpleNamespace(generated_name="committed")
        superseding = SimpleNamespace(generated_name="superseding")
        shell = _fake_shell(committed)

        opener = self._opener(shell)
        shell._safe_import_result = superseding

        rendered = []
        with patch.object(
            app_shell_module.safe_import,
            "open_result",
            lambda target: rendered.append(target._safe_import_result),
        ):
            opener()

        self.assertEqual(rendered, [committed])

    def test_the_live_result_is_put_back_after_the_render(self) -> None:
        # The pin is for the duration of one render, not a state change: the
        # shell's own attribute is what every later screen rebuild reads.
        committed = SimpleNamespace(generated_name="committed")
        superseding = SimpleNamespace(generated_name="superseding")
        shell = _fake_shell(committed)

        opener = self._opener(shell)
        shell._safe_import_result = superseding

        with patch.object(
            app_shell_module.safe_import, "open_result", lambda target: None
        ):
            opener()

        self.assertIs(shell._safe_import_result, superseding)

    def test_a_raising_render_still_puts_the_live_result_back(self) -> None:
        committed = SimpleNamespace(generated_name="committed")
        superseding = SimpleNamespace(generated_name="superseding")
        shell = _fake_shell(committed)

        opener = self._opener(shell)
        shell._safe_import_result = superseding

        def boom(target):
            raise SystemError("no DPG context")

        with patch.object(app_shell_module.safe_import, "open_result", boom):
            with self.assertRaises(SystemError):
                opener()

        self.assertIs(shell._safe_import_result, superseding)

    def test_an_unreplaced_result_still_renders(self) -> None:
        # Anti-vacuity: the ordinary path, where nothing intervenes, must be
        # unchanged rather than merely un-crashing.
        committed = SimpleNamespace(generated_name="committed")
        shell = _fake_shell(committed)

        rendered = []
        with patch.object(
            app_shell_module.safe_import,
            "open_result",
            lambda target: rendered.append(target._safe_import_result),
        ):
            self._opener(shell)()

        self.assertEqual(rendered, [committed])
        self.assertIs(shell._safe_import_result, committed)


if __name__ == "__main__":
    unittest.main()
