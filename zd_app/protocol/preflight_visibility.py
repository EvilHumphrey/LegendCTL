r"""Preflight Windows/public-device visibility before a secondary-interface attempt.

This helper exists to separate:

- device not visible to Windows / official app
- public interface visible
- official app in a usable state for `Device Settings`

from actual hidden-transport negatives.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from zd_app.protocol.hid_transport import PUBLIC_IDENTIFY_OPEN_PROFILE, filter_hid_paths
from zd_app.protocol.trigger_interface import (
    HIDDEN_PRODUCT_ID,
    HIDDEN_VENDOR_ID,
    PublicProbeResult,
    public_hid_paths,
    run_public_identify_probe_candidates,
    run_public_identify_probe_candidates_with_retry,
)


__all__ = [
    "APP_PATH_ENV_VAR",
    "DEFAULT_APP",
    "DEFAULT_UI_PROBE",
    "OfficialUiProbe",
    "SCRIPT_DIR",
    "VisibilityPreflightResult",
    "main",
    "parse_args",
    "print_result",
    "run_official_ui_probe",
    "summarize_preflight",
]


def _script_dir() -> Path:
    """Directory holding the bundled probe ``.ps1``.

    Mirrors the frozen-path resolver in ``ui/fonts.py`` / ``ui/screens/about.py``:
    under PyInstaller the ``.ps1`` is collected to
    ``_MEIPASS/zd_app/protocol`` (see ``pyinstaller_main_zd.spec`` datas), so
    resolve against ``sys._MEIPASS`` when frozen rather than ``__file__``.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "zd_app" / "protocol"
    return Path(__file__).resolve().parent


SCRIPT_DIR = _script_dir()
DEFAULT_UI_PROBE = SCRIPT_DIR / "probe_official_connection_state.ps1"

# Absolute path to Windows PowerShell (avoids a PATH search-order hijack).
_POWERSHELL_EXE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)

# Default `None` because the path varies per user. Callers that need to launch
# the app or pass `-AppPath` to the preflight .ps1 should supply the path
# explicitly (`app_path=...`) or set the `ZD_OFFICIAL_APP_PATH` env var.
DEFAULT_APP: Path | None = None
APP_PATH_ENV_VAR = "ZD_OFFICIAL_APP_PATH"


def _resolve_app_path(app_path: Path | None = None) -> Path | None:
    if app_path is not None:
        return app_path
    env = os.environ.get(APP_PATH_ENV_VAR)
    if env:
        return Path(env)
    return None


@dataclass(frozen=True)
class OfficialUiProbe:
    app_running: bool
    launched: bool
    main_window: str | None
    settings_window: str | None
    device_settings_button: bool
    no_device_connected: bool
    connect_via_usb: bool
    visible_names: tuple[str, ...]


@dataclass(frozen=True)
class VisibilityPreflightResult:
    launch_official: bool
    official_ui: OfficialUiProbe
    public_paths: tuple[str, ...]
    public_probe: PublicProbeResult
    hidden_visible_paths: tuple[str, ...]
    state: str
    summary: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Windows/app visibility before a secondary-interface attempt.")
    parser.add_argument(
        "--launch-official",
        action="store_true",
        help="Launch the official app if it is not already running before probing UI state.",
    )
    parser.add_argument(
        "--app-path",
        type=Path,
        default=None,
        help=(
            "Path to the official app executable if the UI probe needs to launch it. "
            f"Defaults to ${APP_PATH_ENV_VAR} when set; otherwise the .ps1's own default."
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional JSON output path for the preflight result.",
    )
    return parser.parse_args(argv)


def run_official_ui_probe(
    launch_official: bool,
    app_path: Path | None = None,
) -> OfficialUiProbe:
    resolved = _resolve_app_path(app_path)
    if launch_official and resolved is None:
        raise ValueError(
            f"launch_official=True requires app_path or {APP_PATH_ENV_VAR} env var"
        )

    command = [
        _POWERSHELL_EXE,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(DEFAULT_UI_PROBE),
    ]
    if resolved is not None:
        command.extend(["-AppPath", str(resolved)])
    if launch_official:
        command.append("-LaunchIfMissing")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Official UI probe failed.").strip())

    payload = json.loads((result.stdout or "{}").strip() or "{}")
    if not isinstance(payload, dict):
        payload = {}

    return OfficialUiProbe(
        app_running=bool(payload.get("app_running")),
        launched=bool(payload.get("launched")),
        main_window=_as_optional_str(payload.get("main_window")),
        settings_window=_as_optional_str(payload.get("settings_window")),
        device_settings_button=bool(payload.get("device_settings_button")),
        no_device_connected=bool(payload.get("no_device_connected")),
        connect_via_usb=bool(payload.get("connect_via_usb")),
        visible_names=tuple(str(item) for item in payload.get("visible_names") or [] if item),
    )


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def summarize_preflight(
    launch_official: bool,
    official_ui: OfficialUiProbe,
    public_paths: list[str],
    public_probe: PublicProbeResult,
    hidden_visible_paths: list[str],
) -> VisibilityPreflightResult:
    if public_probe.ok and official_ui.device_settings_button:
        state = "ready_for_settings"
        summary = (
            "The public ZD interface is visible and the official app shows the Device Settings entry, "
            "so this is a good time to attempt the secondary interface."
        )
    elif public_probe.ok and official_ui.no_device_connected:
        state = "public_visible_ui_mismatch"
        summary = (
            "Windows can probe the public ZD interface, but the official app still shows No ZD device connected. "
            "Treat this as an app/device-visibility mismatch, not a hidden-transport result."
        )
    elif public_probe.ok:
        state = "public_visible"
        summary = (
            "Windows can probe the public ZD interface, but the official UI is not yet clearly actionable for Device Settings."
        )
    elif not public_paths and official_ui.no_device_connected:
        state = "device_missing"
        summary = (
            "Neither Windows nor the official app currently sees the ZD controller. "
            "Do not treat any failed hidden-transport run in this state as protocol evidence."
        )
    elif not public_paths and official_ui.app_running:
        state = "app_only_no_public_device"
        summary = (
            "The official app is open, but the public ZD interface is not visible to Windows. "
            "This is still an environment/device-visibility boundary."
        )
    elif not public_paths and not official_ui.app_running:
        state = "no_app_no_device"
        summary = (
            "The official app is not open and the public ZD interface is not visible. "
            "There is nothing transport-meaningful to probe yet."
        )
    else:
        state = "unclear"
        summary = (
            "The current Windows/app visibility state is mixed. Check the raw preflight fields before interpreting any transport result."
        )

    return VisibilityPreflightResult(
        launch_official=launch_official,
        official_ui=official_ui,
        public_paths=tuple(public_paths),
        public_probe=public_probe,
        hidden_visible_paths=tuple(hidden_visible_paths),
        state=state,
        summary=summary,
    )


def print_result(result: VisibilityPreflightResult) -> None:
    print("Preflight Transport Visibility")
    print()
    print(f"Launch official: {result.launch_official}")
    print(f"Official app running: {result.official_ui.app_running}")
    print(f"Official launched by probe: {result.official_ui.launched}")
    print(f"Main window: {result.official_ui.main_window or 'none'}")
    print(f"Settings window: {result.official_ui.settings_window or 'none'}")
    print(f"Device Settings button: {result.official_ui.device_settings_button}")
    print(f"No ZD device connected: {result.official_ui.no_device_connected}")
    print(f"Connect via USB visible: {result.official_ui.connect_via_usb}")
    print(f"Public path count: {len(result.public_paths)}")
    print(f"Public probe ok: {result.public_probe.ok}")
    print(f"Public probe path: {result.public_probe.path or 'none'}")
    print(f"Hidden visible path count: {len(result.hidden_visible_paths)}")
    print(f"State: {result.state}")
    print(f"Summary: {result.summary}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    official_ui = run_official_ui_probe(args.launch_official, args.app_path)
    public_paths = public_hid_paths()
    public_probe = run_public_identify_probe_candidates(
        public_paths,
        mode_name="read_write",
        share_mode=PUBLIC_IDENTIFY_OPEN_PROFILE.share_mode,
        creation_disposition=PUBLIC_IDENTIFY_OPEN_PROFILE.creation_disposition,
        flags_and_attributes=PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes,
    )
    hidden_visible_paths = filter_hid_paths(HIDDEN_VENDOR_ID, HIDDEN_PRODUCT_ID)
    result = summarize_preflight(
        launch_official=bool(args.launch_official),
        official_ui=official_ui,
        public_paths=public_paths,
        public_probe=public_probe,
        hidden_visible_paths=hidden_visible_paths,
    )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")

    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
