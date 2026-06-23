"""Entry point for the ZD Ultimate Legend MVP control center.

Default boot is stock mode: no dual-mode auto-trigger.

Architectural note (2026-04-30): v1's StandaloneTriggerService activates
VID_20BC dual-mode at boot, which hides the standard MI_02 HID interface
from PnP enumeration. SettingsService writes settings through MI_02, so it
cannot function while dual-mode is active.

Operator workflow is settings-first and stock-mode based: open wrapper,
adjust firmware-persisted settings, close wrapper, then play. Polling rate
and other settings remain applied after the wrapper exits, so production
boot keeps MI_02 visible and SettingsService functional.

StandaloneTriggerService remains in ``zd_app/services/`` for opt-in code
paths that genuinely need VID_20BC dual-mode held active. The default
entry point does not currently need it.

The VMware Workstation USB-redirect warning is preserved because VMware can
still intercept USB device traffic regardless of which service drives the
controller.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zd_app.services import crash_reporter
from zd_app.services.device_service import DeviceService
from zd_app.services.diagnostics_service import DiagnosticsService
from zd_app.services.path_scrub import PathScrubbingFormatter
from zd_app.services.profile_service import ProfileService
from zd_app.services.settings_service import SettingsService, SetPollingRateOutcome
from zd_app.services.diagnostic_bundle import DiagnosticBundleService
from zd_app.services.module_passport import ModulePassportService
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.storage.last_applied_store import LastAppliedStore
from zd_app.storage.profile_store import ProfileStore
from zd_app.storage.settings_store import SettingsStore, initialize_user_data_dir
from zd_app.storage.wrapper_profile_store import WrapperProfileStore
from zd_app.ui.app_shell import AppShell, threaded_hid_executor


logger = logging.getLogger(__name__)


def _warn_if_vmware_usb_redirect_active() -> None:
    """Pre-warn the operator if VMware Workstation may intercept USB."""
    from zd_app.services.host_environment import warn_if_vmware_usb_redirect_active

    warn_if_vmware_usb_redirect_active()


def _settings_service_watchdog(
    shell: AppShell,
    settings_service: SettingsService,
    stop_event: threading.Event,
    *,
    retry_interval: float = 2.0,
) -> None:
    """Retry SettingsService.start() until a controller appears."""

    while not stop_event.wait(retry_interval):
        if shell.settings_service is not None:
            continue
        outcome = settings_service.start()
        if outcome == SetPollingRateOutcome.OK:
            logger.info("SettingsService now available; controller connected")
            shell.attach_settings_service(settings_service)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Install crash handlers BEFORE any further startup work — DPG init,
    # SettingsService start, and thread spawns can all surface the kinds of
    # failures the reporter exists to capture.
    user_data_dir = initialize_user_data_dir()
    crash_reporter.install_crash_handlers(user_data_dir)

    # File-based INFO log. PyInstaller --windowed builds discard stderr, so
    # the basicConfig stream handler above goes nowhere visible in production.
    # Rotate at 5 MB, keep 3 backups — enough headroom for ops triage without
    # filling the user-data dir.
    try:
        log_dir = user_data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "zd_wrapper.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        # Scrub absolute home paths (username included) from every line written
        # to the rotating app log — SUPPORT.md asks users to attach this file to
        # bug reports, and startup / service-constructor lines log absolute
        # paths. One handler-level formatter catches all sources, including
        # logger.exception() tracebacks.
        file_handler.setFormatter(
            PathScrubbingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
        logger.info("File logger attached at %s", log_dir / "zd_wrapper.log")
    except OSError:
        logger.exception("Failed to attach file logger; continuing with stderr-only logging")

    settings_store = SettingsStore()
    settings_store.load()  # Load the on-disk settings (empty AppSettings if first run).

    settings_service = SettingsService()
    try:
        settings_start = settings_service.start()
        settings_service_for_ui = settings_service
        if settings_start != SetPollingRateOutcome.OK:
            logger.warning(
                "SettingsService failed to start: %s; settings UI will be limited",
                settings_start.value,
            )
            settings_service_for_ui = None

        _warn_if_vmware_usb_redirect_active()

        logger.info(
            "Starting AppShell in stock mode; VID_20BC dual-mode trigger is not auto-run."
        )

        wear_ledger_service = WearLedgerService(base_dir=user_data_dir / "wear_ledger")
        module_passport_service = ModulePassportService(
            base_dir=user_data_dir / "module_passport",
            wear_ledger=wear_ledger_service,
        )
        diagnostic_bundle_service = DiagnosticBundleService(
            base_dir=user_data_dir / "diagnostic_bundles",
            module_passport_service=module_passport_service,
            health_report_dir=user_data_dir / "health_reports",
            wear_ledger=wear_ledger_service,
            app_data_dir=user_data_dir,
        )

        app = AppShell(
            device_service=DeviceService(),
            profile_service=ProfileService(ProfileStore()),
            diagnostics_service=DiagnosticsService(),
            settings_store=settings_store,
            settings_service=settings_service_for_ui,
            wrapper_profile_store=WrapperProfileStore(),
            # Device-vs-Profile Phase 2: the apply pipeline records what it
            # last sent here; tests construct AppShell without this store and
            # the recording hooks stay no-ops.
            last_applied_store=LastAppliedStore(),
            wear_ledger_service=wear_ledger_service,
            module_passport_service=module_passport_service,
            diagnostic_bundle_service=diagnostic_bundle_service,
            # Production runs the long HID flows (footer Read, profile apply,
            # RP restore) on a worker thread so the render loop keeps
            # animating; tests construct AppShell without an executor and get
            # the synchronous inline behavior.
            hid_executor=threaded_hid_executor,
        )
        stop_event = threading.Event()
        watchdog = None
        if settings_service_for_ui is None:
            watchdog = threading.Thread(
                target=_settings_service_watchdog,
                args=(app, settings_service, stop_event),
                daemon=True,
            )
            watchdog.start()
        try:
            app.run()
            return 0
        finally:
            stop_event.set()
            if watchdog is not None:
                watchdog.join(timeout=3.0)
    finally:
        settings_service.stop()


if __name__ == "__main__":
    sys.exit(main())
