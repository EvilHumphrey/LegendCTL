"""Layering-regression tests for the UI-neutral label helpers.

Pins the layering fix: ``gate_decision_label`` lives
in ``zd_app/i18n/labels.py``, not ``zd_app/ui/trust_labels.py``, so services
can call it without a UI dependency.
"""

from __future__ import annotations

import inspect
import unittest


class LabelModuleLayeringTests(unittest.TestCase):
    def test_gate_decision_label_importable_from_i18n(self) -> None:
        from zd_app.i18n.labels import gate_decision_label  # noqa: F401

    def test_unknown_gate_label_constant_importable_from_i18n(self) -> None:
        from zd_app.i18n.labels import UNKNOWN_GATE_DECISION_LABEL  # noqa: F401

    def test_preflight_service_does_not_import_from_ui(self) -> None:
        import zd_app.services.preflight_service as ps

        src = inspect.getsource(ps)
        self.assertNotIn("from zd_app.ui", src)
        self.assertNotIn("import zd_app.ui", src)


if __name__ == "__main__":
    unittest.main()
