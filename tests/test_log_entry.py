"""Tests for the shared LogEntry type + render helpers.

Pins the LogEntry consolidation: LogEntry +
render_log_entry + render_log_message live in zd_app.services._log_entry
and the previously-duplicated copies in device_service / diagnostics_service
are gone. Documents the ``_key``-suffix convention.
"""

from __future__ import annotations

import unittest

from zd_app.i18n import set_locale
from zd_app.services._log_entry import (
    LogEntry,
    _render_log_fmt_args,
    render_log_entry,
    render_log_message,
)


class LogEntryRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        set_locale("en")

    def test_string_entries_pass_through_unchanged(self) -> None:
        self.assertEqual(render_log_entry("legacy raw entry"), "legacy raw entry")
        self.assertEqual(render_log_message("legacy raw entry"), "legacy raw entry")

    def test_render_log_entry_includes_timestamp(self) -> None:
        entry = LogEntry(timestamp="14:00:00", key="apply.profile.success", fmt_args={"name": "Apex Final", "n": 28})
        rendered = render_log_entry(entry)
        self.assertIn("14:00:00", rendered)
        self.assertIn("Apex Final", rendered)

    def test_render_log_message_omits_timestamp(self) -> None:
        entry = LogEntry(timestamp="14:00:00", key="apply.profile.success", fmt_args={"name": "Apex Final", "n": 28})
        rendered = render_log_message(entry)
        self.assertNotIn("14:00:00", rendered)
        self.assertIn("Apex Final", rendered)


class KeySuffixConventionTests(unittest.TestCase):
    def setUp(self) -> None:
        set_locale("en")

    def test_key_suffixed_arg_resolves_through_t(self) -> None:
        """A fmt_arg ``name_key`` whose value is an i18n key should resolve to t(value).

        After expansion, the suffix-stripped name (``name``) holds the
        translated value of ``t("device.summary.source.xinput")``.
        """
        rendered = _render_log_fmt_args({"name_key": "device.summary.source.xinput"})
        self.assertIn("name", rendered)
        # The translation of device.summary.source.xinput in en.json is "XInput".
        self.assertEqual(rendered["name"], "XInput")
        # Suffix-stripped — the original ``name_key`` is gone.
        self.assertNotIn("name_key", rendered)

    def test_non_suffixed_args_pass_through_literally(self) -> None:
        """Args without ``_key`` suffix are substituted as-is, not translated."""
        rendered = _render_log_fmt_args({"name": "raw literal", "n": 28})
        self.assertEqual(rendered["name"], "raw literal")
        self.assertEqual(rendered["n"], 28)

    def test_non_string_key_arg_passes_through(self) -> None:
        """A ``_key``-suffixed arg whose value isn't a string is NOT translated."""
        rendered = _render_log_fmt_args({"count_key": 42})
        # 42 is not a string, so the convention does not apply; pass-through.
        self.assertEqual(rendered["count_key"], 42)


if __name__ == "__main__":
    unittest.main()
