"""Local-first crash reporter for the ZD Ultimate Legend wrapper.

Captures uncaught Python exceptions, threading exceptions, and interpreter
faults (segfault, abort) to sanitized text reports under
``<user_data_dir>/crashes/``. No external dependencies; no PII upload.
A future hosted-upload path is out of scope; this module only produces
local artifacts — ``zd_app/ui/app_shell.py``'s review modal surfaces them
on next launch.

Sanitization
------------
Reports intentionally exclude ``sys.argv``, environment variables, frame
local variables, hostnames, and network info. The exception summary and the
traceback block are run through the shared path scrubber
(:func:`zd_app.services.path_scrub.scrub_paths`) before writing, so a home
path in an exception message or a from-source frame
(``C:\\Users\\<name>\\...``) collapses to a basename / app-relative frame
(``zd_app/...``) and the account username never lands in the report. The
Recent Activity section records ``log_i18n_event`` keys only — never
``fmt_args`` — because ``fmt_args`` may carry profile names or other user
input.
"""

from __future__ import annotations

import collections
import datetime
import faulthandler
import logging
import platform
import sys
import threading
import traceback
from pathlib import Path

from zd_app.services.path_scrub import scrub_paths
from zd_app.version import __app_id__, __app_name__, __build_commit__, __version__


logger = logging.getLogger(__name__)

_LOG_BUFFER_SIZE = 50

# Module-level state (intentional; one process = one crash reporter).
_log_buffer: collections.deque[tuple[str, str]] = collections.deque(maxlen=_LOG_BUFFER_SIZE)
_installed = False
_previous_excepthook = None
_previous_threading_excepthook = None
_faulthandler_log_handle = None  # Held to keep the file open until process exit
_crashes_dir: Path | None = None
_native_filter_callback = None  # Held to keep the WINFUNCTYPE callback alive for SEH
_native_hook_installed = False

# PRIVACY TRIPWIRE (do not flip without reading the PRIVACY GATE note in
# install_crash_handlers): _install_native_windows_hook writes UNSANITIZED
# full-memory .dmp files. This constant keeps it inert even if its deferred
# call site is uncommented — re-enabling requires deliberately setting this
# True AND first gating dump generation behind an explicit, off-by-default
# user opt-in. Never default this to True, and never auto-upload the .dmp.
_MINIDUMP_OPT_IN = False


# Name of the in-folder marker that warns the user the crashes directory is
# not share-safe as-is. The leading underscore keeps it out of
# ``list_unread_crash_reports`` (same convention as ``_faulthandler.log``), so
# the marker never surfaces in the in-app "unread crash reports" review.
_CRASHES_README_FILENAME = "_README.txt"


def _write_crashes_readme(crashes_dir: Path) -> None:
    """Drop a marker explaining the crashes folder is not share-safe as-is.

    The in-app "Open report folder" button points the user straight at this
    directory, which can hold C-level artifacts (``_faulthandler.log``, and any
    future ``.dmp``) that are written without sanitization and may contain raw
    home paths / the account username. Best-effort: a marker-write failure must
    never block crash-handler installation.
    """

    marker = crashes_dir / _CRASHES_README_FILENAME
    body = (
        f"{__app_name__} - local crash diagnostics\n"
        "==========================================\n\n"
        "Nothing in this folder is uploaded anywhere automatically.\n\n"
        "NOT SHARE-SAFE AS-IS:\n"
        "  '_faulthandler.log' (and any '.dmp' files a future build may add)\n"
        "  is written at the C level during a crash and is NOT sanitized. It\n"
        "  can contain raw filesystem paths, including your Windows account\n"
        "  name (e.g. C:\\Users\\<you>\\...).\n\n"
        "SHARE-SAFE:\n"
        "  The dated '*.txt' crash reports here ARE sanitized - home paths are\n"
        "  reduced to file basenames. Prefer those if you send a report.\n"
    )
    try:
        marker.write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write crashes README marker: %s", exc)


def install_crash_handlers(user_data_dir: Path) -> None:
    """Install sys.excepthook + threading.excepthook + faulthandler.

    Crash reports go to ``<user_data_dir>/crashes/<iso-timestamp>.txt`` as
    sanitized text. Idempotent — calling twice is harmless. Should be
    called once at app startup, BEFORE dearpygui's ``setup_dearpygui()``
    / ``create_context()`` calls so any DPG-side error during init also
    captures.
    """

    global _installed, _previous_excepthook, _previous_threading_excepthook
    global _faulthandler_log_handle, _crashes_dir

    crashes_dir = Path(user_data_dir) / "crashes"
    crashes_dir.mkdir(parents=True, exist_ok=True)
    _crashes_dir = crashes_dir
    # Always (re)write the marker so it exists whenever the folder does, even
    # on the idempotent second call. Cheap and best-effort.
    _write_crashes_readme(crashes_dir)

    if _installed:
        return

    _previous_excepthook = sys.excepthook
    sys.excepthook = _excepthook

    _previous_threading_excepthook = threading.excepthook
    threading.excepthook = _threading_excepthook

    fault_log_path = crashes_dir / "_faulthandler.log"
    # Open mode "w" (truncate per startup), not "a": a faulthandler dump is only
    # useful for the crash that just happened, and this file is written at the C
    # level — unsanitized, with raw frame paths incl. C:\Users\<name> on
    # from-source builds. Truncating each launch stops historical native stacks
    # (and their home paths) from accumulating in a non-share-safe file.
    # buffering=1 (line-buffered) so segfault traces flush mid-crash.
    _faulthandler_log_handle = open(fault_log_path, "w", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_faulthandler_log_handle)

    if sys.platform == "win32":
        # The native SEH hook (SetUnhandledExceptionFilter + MiniDumpWriteDump)
        # was reverted after the crash was localized to a reproducible
        # SIGSEGV during DPG import when the hook is installed.
        #
        # Empirical matrix that localized this (2026-05-09 isolation diagnostics):
        #   - control (no crash handlers, then `import dearpygui`)              : OK
        #   - sys.excepthook + threading.excepthook only                        : OK
        #   - faulthandler only                                                 : OK
        #   - SetUnhandledExceptionFilter with no-op return-0 filter            : OK
        #   - full install_crash_handlers (excepthook + faulthandler + SEH +
        #     dbghelp.MiniDumpWriteDump argtypes/restype setup), then DPG import: SIGSEGV 139
        #   - same as above MINUS the SEH/MiniDumpWriteDump hook                : OK + DPG renders 430 frames
        # Hypothesis: dbghelp.dll loaded by the WINFUNCTYPE/argtypes setup for
        # MiniDumpWriteDump alters Windows error-handling state in a way that
        # DPG/GLFW's subsequent OpenGL DLL chain can't recover from. Confirmation
        # requires WinDbg attach on the failing run.
        #
        # The _install_native_windows_hook function body below is intentionally
        # preserved so the re-enable patch is a one-line uncomment once the
        # dbghelp/DPG DLL ordering is understood.
        #
        # PRIVACY GATE (do not skip on re-enable): MiniDumpWriteDump below
        # captures a full-memory .dmp of the live process. Unlike the text
        # crash reports (which are sanitized and store only i18n KEYS, never
        # user input — see record_log_entry), a minidump contains UNSANITIZED
        # process memory: the controller's PnP instance ID, in-flight HID
        # buffers, and profile names. These .dmp files must NEVER be auto-
        # uploaded anywhere. Re-enabling this hook is therefore NOT just an
        # uncomment: dump generation must first be gated behind an explicit,
        # off-by-default user opt-in, and any future upload path requires
        # separate, informed consent.
        logger.debug("Native Windows crash hook deferred (see comment).")

    _installed = True
    logger.info("Crash handlers installed; reports go to %s", crashes_dir)


def record_log_entry(key: str, fmt_args: dict | None = None) -> None:
    """Append a log entry's KEY to the rolling crash-report buffer.

    ``fmt_args`` is accepted for API symmetry with ``log_i18n_event`` but is
    intentionally NOT stored — Recent Activity entries can carry user input
    (profile names, exported file paths) and would leak into the crash
    report. The buffer caps at 50 entries (FIFO).
    """

    del fmt_args  # See docstring.
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_buffer.append((timestamp, key))


def list_unread_crash_reports(
    user_data_dir: Path,
    since: datetime.datetime | None = None,
) -> list[Path]:
    """Return ``.txt`` crash reports written after ``since`` (or all if None).

    Files with an adjacent ``.txt.reviewed`` marker are excluded. The
    ``_faulthandler.log`` rolling file and the ``_README.txt`` privacy marker
    are excluded by the leading-underscore convention. Sorted chronologically
    (ISO timestamp filename prefix).
    """

    crashes_dir = Path(user_data_dir) / "crashes"
    if not crashes_dir.is_dir():
        return []

    reports: list[Path] = []
    for entry in crashes_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".txt":
            continue
        if entry.name.startswith("_"):
            continue
        marker = entry.with_name(entry.name + ".reviewed")
        if marker.exists():
            continue
        if since is not None:
            try:
                mtime = datetime.datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=datetime.timezone.utc
                )
            except OSError:
                continue
            if mtime <= since:
                continue
        reports.append(entry)

    reports.sort()
    return reports


def mark_crashes_reviewed(user_data_dir: Path, files: list[Path]) -> None:
    """Write an adjacent ``.txt.reviewed`` marker for each given file.

    Failures (e.g. permission errors) are logged at WARNING and swallowed —
    a missing marker just means the file resurfaces in the next review,
    which is a recoverable nuisance.
    """

    del user_data_dir  # ``files`` already carries absolute paths.
    for f in files:
        marker = f.with_name(f.name + ".reviewed")
        try:
            marker.write_text("", encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write reviewed marker for %s: %s", f, exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    try:
        _write_crash_report(
            exc_type, exc_value, exc_tb,
            source="excepthook",
            thread_name="MainThread",
        )
    except Exception:  # noqa: BLE001 - never let the hook itself crash
        logger.exception("crash_reporter excepthook failed to write report")
    if _previous_excepthook is not None:
        try:
            _previous_excepthook(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001
            logger.exception("crash_reporter excepthook chain failed")


def _threading_excepthook(args) -> None:
    """``args`` is a ``threading.ExceptHookArgs`` named tuple."""

    try:
        thread = getattr(args, "thread", None)
        thread_name = thread.name if thread is not None else "<unknown>"
        _write_crash_report(
            args.exc_type, args.exc_value, args.exc_traceback,
            source="threading",
            thread_name=thread_name,
        )
    except Exception:  # noqa: BLE001
        logger.exception("crash_reporter threading_excepthook failed to write report")
    if _previous_threading_excepthook is not None:
        try:
            _previous_threading_excepthook(args)
        except Exception:  # noqa: BLE001
            logger.exception("crash_reporter threading_excepthook chain failed")


def _write_crash_report(exc_type, exc_value, exc_tb, *, source: str, thread_name: str) -> None:
    if _crashes_dir is None:
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    iso_basename = now.strftime("%Y%m%dT%H%M%SZ")

    body = _format_report(now, exc_type, exc_value, exc_tb, source=source, thread_name=thread_name)

    # Exclusive-create so two crashes in the same wall-clock second (the
    # main-thread and worker-thread excepthooks are unsynchronized) don't collide
    # on one filename and silently overwrite each other — the later, often a
    # cascade, would clobber the earlier root-cause report. open(..., "x") is
    # atomic (O_EXCL), so even concurrent writers each land on a distinct name.
    crash_path = _crashes_dir / f"{iso_basename}.txt"
    attempt = 1
    while True:
        try:
            with open(crash_path, "x", encoding="utf-8") as handle:
                handle.write(body)
            return
        except FileExistsError:
            crash_path = _crashes_dir / f"{iso_basename}_{attempt}.txt"
            attempt += 1
            if attempt > 10000:  # pathological backstop — never spin forever
                logger.error("Could not find a free crash-report name for %s", iso_basename)
                return
        except OSError:
            logger.exception("Could not write crash report to %s", crash_path)
            return


def _format_report(
    timestamp: datetime.datetime,
    exc_type,
    exc_value,
    exc_tb,
    *,
    source: str,
    thread_name: str,
) -> str:
    iso = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    dpg_version = _resolve_dpg_version()
    os_label = _resolve_os_label()

    if exc_type is not None:
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        # Scrub home paths from BOTH the frame paths in the traceback and the
        # exception summary (the exception *value* can be a path, e.g.
        # FileNotFoundError(r"C:\\Users\\<name>\\x.json")). The shared scrubber
        # preserves app-relative frames (``zd_app/...``) while dropping the
        # home/username prefix.
        tb_block = scrub_paths("".join(tb_lines).rstrip())
        exc_summary = scrub_paths(
            f"{getattr(exc_type, '__module__', '?')}.{exc_type.__name__}: {exc_value}"
        )
    else:
        tb_block = "(no traceback)"
        exc_summary = "(none)"

    header = f"{__app_name__} — crash report"
    sections = [
        header,
        "=" * len(header),
        f"Timestamp:     {iso}",
        f"App version:   {__version__}",
        f"Build commit:  {__build_commit__ or '(dev)'}",
        f"App ID:        {__app_id__}",
        f"OS:            {os_label}",
        f"Python:        {platform.python_version()}",
        f"Dear PyGui:    {dpg_version}",
        f"Source:        {source}",
        f"Thread:        {thread_name}",
        "",
        "Exception",
        "---------",
        exc_summary,
        "",
        "Traceback",
        "---------",
        tb_block,
        "",
        "Recent activity (last 50 LogEntry items, sanitized — keys only)",
        "----------------------------------------------------------------",
    ]
    if _log_buffer:
        for ts, key in _log_buffer:
            sections.append(f"{ts}  {key}")
    else:
        sections.append("(no events captured)")

    return "\n".join(sections) + "\n"


def _resolve_dpg_version() -> str:
    try:
        import dearpygui  # type: ignore[import-not-found]

        return str(getattr(dearpygui, "__version__", "unknown"))
    except ImportError:
        return "(not installed)"


def _resolve_os_label() -> str:
    return f"{platform.system()} {platform.release()} ({platform.version()}) {platform.machine()}"


# ---------------------------------------------------------------------------
# Windows native crash hook — best-effort minidump on SEH faults
# ---------------------------------------------------------------------------


def _install_native_windows_hook(crashes_dir: Path) -> None:
    """Register a SetUnhandledExceptionFilter callback that writes a minidump.

    The callback returns EXCEPTION_CONTINUE_SEARCH so the OS default handler
    still runs after our minidump write — that preserves Windows Error
    Reporting / WinDbg integration. Best-effort: any failure inside the
    callback is silently swallowed so we never replace a crash with a
    different crash.
    """

    # Privacy tripwire: refuse to arm the minidump hook unless opt-in is
    # explicitly enabled (see _MINIDUMP_OPT_IN). Fail closed before any work.
    if not _MINIDUMP_OPT_IN:
        return

    global _native_filter_callback, _native_hook_installed

    if _native_hook_installed:
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    dbghelp = ctypes.windll.dbghelp

    EXCEPTION_CONTINUE_SEARCH = 0
    GENERIC_WRITE = 0x40000000
    CREATE_ALWAYS = 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    MINIDUMP_TYPE_NORMAL = 0x00000000

    # WINFUNCTYPE for the SEH top-level filter callback signature:
    # LONG (WINAPI *PTOP_LEVEL_EXCEPTION_FILTER)(EXCEPTION_POINTERS*)
    TopLevelFilterType = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

    kernel32.SetUnhandledExceptionFilter.argtypes = [TopLevelFilterType]
    kernel32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    dbghelp.MiniDumpWriteDump.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL

    @TopLevelFilterType
    def _filter(_exc_pointers: int) -> int:
        try:
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            # This .dmp holds UNSANITIZED full-process memory — never auto-upload
            # it. See the PRIVACY GATE note at the deferred call site above.
            dump_path = crashes_dir / f"{timestamp}.dmp"
            handle = kernel32.CreateFileW(
                str(dump_path),
                GENERIC_WRITE,
                0,
                None,
                CREATE_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle in (None, 0, INVALID_HANDLE_VALUE):
                return EXCEPTION_CONTINUE_SEARCH
            try:
                dbghelp.MiniDumpWriteDump(
                    kernel32.GetCurrentProcess(),
                    kernel32.GetCurrentProcessId(),
                    handle,
                    MINIDUMP_TYPE_NORMAL,
                    None, None, None,
                )
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - swallow; never replace a crash with another crash
            pass
        return EXCEPTION_CONTINUE_SEARCH

    # Hold the callback at module scope so Python's GC doesn't collect it
    # while Windows still has the function pointer registered.
    _native_filter_callback = _filter
    kernel32.SetUnhandledExceptionFilter(_filter)
    _native_hook_installed = True


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Reset module-level state so tests can install fresh handlers per case.

    Tests should not normally need this — the production install is
    idempotent. But test isolation requires clearing the install flag and
    closing the faulthandler file handle.
    """

    global _installed, _previous_excepthook, _previous_threading_excepthook
    global _faulthandler_log_handle, _crashes_dir
    global _native_filter_callback, _native_hook_installed

    if _previous_excepthook is not None:
        sys.excepthook = _previous_excepthook
    if _previous_threading_excepthook is not None:
        threading.excepthook = _previous_threading_excepthook
    if _faulthandler_log_handle is not None:
        try:
            faulthandler.disable()
            _faulthandler_log_handle.close()
        except OSError:
            pass

    _log_buffer.clear()
    _installed = False
    _previous_excepthook = None
    _previous_threading_excepthook = None
    _faulthandler_log_handle = None
    _crashes_dir = None
    # Native callback is intentionally NOT cleared from Windows — once
    # SetUnhandledExceptionFilter is called we cannot reliably un-register
    # it from a Python callback context without risking SEH races. The
    # module-level flag is reset so subsequent installs re-do the bind.
    _native_filter_callback = None
    _native_hook_installed = False
