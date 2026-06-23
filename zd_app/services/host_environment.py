"""Host-environment heuristics for the wrapper's startup checks.

Right now this is a single concern: warn the operator before launching
vendor.exe if VMware Workstation is running and likely to intercept the
controller's USB device. We don't inspect VMware's USB-redirect setting
itself (too version-dependent); the presence of a running VM is a
strong-enough heuristic to justify a heads-up.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Callable, Optional

from zd_app.services._subprocess_helpers import silent_run


logger = logging.getLogger(__name__)


VMWARE_VM_PROCESS_NAME = "vmware-vmx.exe"

# Absolute path to tasklist.exe (avoids a PATH search-order hijack).
_TASKLIST_EXE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe"
)


def _default_process_lister() -> list[str]:
    """Return a list of running process image names via ``tasklist``."""
    result = silent_run(
        [_TASKLIST_EXE, "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "INFO" in line.upper():
            continue
        # CSV row: "image name","pid","session","sess#","mem"
        first = line.split(",", 1)[0].strip().strip('"')
        if first:
            names.append(first)
    return names


def is_vmware_workstation_running(
    process_lister: Optional[Callable[[], list[str]]] = None,
) -> bool:
    """Return True if a running ``vmware-vmx.exe`` is detected.

    A running ``vmware-vmx.exe`` indicates an active VM, which is when the
    USB-redirect popup actually fires. VMware Workstation being installed
    or its services running isn't sufficient on its own.
    """
    lister = process_lister or _default_process_lister
    try:
        names = lister()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("VMware probe failed: %s", exc)
        return False
    return any(name.lower() == VMWARE_VM_PROCESS_NAME for name in names)


def _default_write_warning(msg: str) -> None:
    sys.stderr.write(msg)


def warn_if_vmware_usb_redirect_active(
    process_lister: Optional[Callable[[], list[str]]] = None,
    write_warning: Optional[Callable[[str], None]] = None,
) -> bool:
    """Emit a non-blocking warning if a VMware VM is running.

    Returns True if the warning was emitted, False otherwise. This does NOT
    auto-modify VMware's config — modifying the host's VMware setup is
    invasive and version-fragile. The warning is informational only.
    """
    if not is_vmware_workstation_running(process_lister):
        return False
    writer = write_warning or _default_write_warning
    writer(
        "Warning: VMware Workstation has an active VM. If its USB-redirect\n"
        "setting is enabled, vendor.exe startup may pop a 'connect device to\n"
        "host or VM?' dialog. Dismiss it as 'host' so the wrapper can see the\n"
        "controller.\n"
    )
    return True
