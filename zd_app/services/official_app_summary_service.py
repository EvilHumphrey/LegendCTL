"""Read-only UI Automation bridge for the official ZD Windows app summary."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

from zd_app.services._subprocess_helpers import silent_run


# Absolute path to Windows PowerShell (avoids a PATH search-order hijack).
_POWERSHELL_EXE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)


WINDOW_NAME_PATTERNS = (
    "Controller Settings",
    "Controller Setting",
)

SUMMARY_PATTERNS = {
    "battery_level": (
        re.compile(r"^(?:Battery|电量):\s*(.+)$", re.IGNORECASE),
    ),
    "firmware_version": (
        re.compile(r"^(?:Version|版本):\s*(.+)$", re.IGNORECASE),
    ),
    "active_onboard_profile": (
        re.compile(r"^(?:Config|配置):\s*(.+)$", re.IGNORECASE),
    ),
    "sleep_setting": (
        re.compile(r"^(?:Sleep|休眠):\s*(.+)$", re.IGNORECASE),
    ),
}

POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::RootElement
$children = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)

$target = $null
for ($index = 0; $index -lt $children.Count; $index++) {
    $candidate = $children.Item($index)
    $name = $candidate.Current.Name
    if ([string]::IsNullOrWhiteSpace($name)) {
        continue
    }
    if (
        $name -eq 'Controller Settings' -or
        $name -like '*Controller Settings*' -or
        $name -like '*Controller Setting*'
    ) {
        $target = $candidate
        break
    }
}

if ($null -eq $target) {
    return
}

$elements = $target.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$names = New-Object 'System.Collections.Generic.List[string]'
for ($index = 0; $index -lt $elements.Count; $index++) {
    $name = $elements.Item($index).Current.Name
    if (-not [string]::IsNullOrWhiteSpace($name)) {
        $names.Add($name)
    }
}

[pscustomobject]@{
    window_title = $target.Current.Name
    names = $names
} | ConvertTo-Json -Compress -Depth 4
"""


@dataclass(frozen=True)
class OfficialAppSummary:
    battery_level: str | None = None
    firmware_version: str | None = None
    active_onboard_profile: int | None = None
    sleep_setting: str | None = None
    window_title: str | None = None


class OfficialAppSummaryService:
    def __init__(
        self,
        timeout_seconds: float = 3.0,
        cache_window_seconds: float = 2.0,
        clock=None,
    ):
        self.timeout_seconds = timeout_seconds
        self.cache_window_seconds = max(0.0, cache_window_seconds)
        self._clock = clock or time.monotonic
        self._cached_summary: OfficialAppSummary | None = None
        self._has_cached_probe = False
        self._last_probe_at = 0.0

    def read_summary(self, force_refresh: bool = False) -> OfficialAppSummary | None:
        now = self._clock()
        if (
            not force_refresh
            and self._has_cached_probe
            and (now - self._last_probe_at) < self.cache_window_seconds
        ):
            return self._cached_summary

        payload = self._run_probe()
        self._last_probe_at = now
        self._has_cached_probe = True
        if payload is None:
            self._cached_summary = None
            return None

        names = [value.strip() for value in payload.get("names", []) if isinstance(value, str) and value.strip()]
        if not names:
            self._cached_summary = None
            return None

        fields: dict[str, str | int | None] = {
            "battery_level": None,
            "firmware_version": None,
            "active_onboard_profile": None,
            "sleep_setting": None,
        }

        for text in names:
            for field_name, patterns in SUMMARY_PATTERNS.items():
                if fields[field_name] is not None:
                    continue
                match = next((pattern.match(text) for pattern in patterns if pattern.match(text)), None)
                if match is None:
                    continue
                value = match.group(1).strip()
                if field_name == "active_onboard_profile":
                    parsed = self._parse_profile_index(value)
                    if parsed is not None:
                        fields[field_name] = parsed
                else:
                    fields[field_name] = value

        if not any(value is not None for value in fields.values()):
            self._cached_summary = None
            return None

        self._cached_summary = OfficialAppSummary(
            battery_level=fields["battery_level"] if isinstance(fields["battery_level"], str) else None,
            firmware_version=fields["firmware_version"] if isinstance(fields["firmware_version"], str) else None,
            active_onboard_profile=fields["active_onboard_profile"] if isinstance(fields["active_onboard_profile"], int) else None,
            sleep_setting=fields["sleep_setting"] if isinstance(fields["sleep_setting"], str) else None,
            window_title=payload.get("window_title"),
        )
        return self._cached_summary

    def clear_cache(self) -> None:
        self._cached_summary = None
        self._has_cached_probe = False
        self._last_probe_at = 0.0

    def _run_probe(self) -> dict | None:
        try:
            result = silent_run(
                [
                    _POWERSHELL_EXE,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    POWERSHELL_SCRIPT,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        output = (result.stdout or "").strip()
        if not output:
            return None

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_profile_index(value: str) -> int | None:
        match = re.search(r"\d+", value)
        if match is None:
            return None
        return int(match.group(0))
