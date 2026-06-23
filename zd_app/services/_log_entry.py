"""Shared structured log-entry type + render helpers.

Used by both ``DeviceService`` (Recent Activity log) and ``DiagnosticsService``
(diagnostics event log). Both stores carry mixed ``str | LogEntry`` for
backward compatibility with legacy raw-string entries; ``render_log_entry``
and ``render_log_message`` accept either form.

The ``_key``-suffix convention:

    LogEntry(key="log.controller_event", fmt_args={"name_key": "device.summary.source.xinput"})

Format-arg keys ending in ``_key`` whose values are themselves i18n keys are
resolved through ``t()`` before substitution into the parent template, then
passed under the suffix-stripped name. The example above renders as
``t("log.controller_event", name=t("device.summary.source.xinput"))``. This
lets a structured entry nest localized fragments inside another localized
template without callers manually translating both layers up front.

Consolidated here from per-service duplicates; previously the same dataclass
+ helpers lived verbatim in ``device_service.py`` and
``diagnostics_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zd_app.i18n import t


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    key: str
    fmt_args: dict[str, Any] = field(default_factory=dict)


def render_log_entry(entry: str | LogEntry) -> str:
    """Render ``entry`` as ``timestamp + space + localized message``."""
    if isinstance(entry, str):
        return entry
    return f"{entry.timestamp}  {render_log_message(entry)}"


def render_log_message(entry: str | LogEntry) -> str:
    """Render the localized message body without the timestamp prefix."""
    if isinstance(entry, str):
        return entry
    return t(entry.key, **_render_log_fmt_args(entry.fmt_args))


def _render_log_fmt_args(fmt_args: dict[str, Any]) -> dict[str, Any]:
    """Apply the ``_key``-suffix convention to ``fmt_args`` (see module docstring)."""
    rendered: dict[str, Any] = {}
    for key, value in fmt_args.items():
        if key.endswith("_key") and isinstance(value, str):
            rendered[key.removesuffix("_key")] = t(value)
        else:
            rendered[key] = value
    return rendered
