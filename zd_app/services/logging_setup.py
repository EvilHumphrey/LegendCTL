"""Single-owner file logging lifecycle for ``zd_wrapper.log``.

Controls whether LegendCTL writes the rotating application log file and at what
level (``Off`` / ``Quiet`` / ``Normal`` / ``Verbose``). Startup buffers early
records in memory until ``SettingsStore.load()`` finishes so ``Off`` never
creates ``logs/`` or writes to disk while enabled modes preserve early
warnings and errors.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from pathlib import Path

from zd_app.models import VALID_LOGGING_VERBOSITIES
from zd_app.services.path_scrub import PathScrubbingFormatter
from zd_app.storage._output_guards import guarded_output_directory
from zd_app.version import __build_commit__, __version__


logger = logging.getLogger(__name__)

LOGGING_VERBOSITY_OFF = "Off"
LOGGING_VERBOSITY_QUIET = "Quiet"
LOGGING_VERBOSITY_NORMAL = "Normal"
LOGGING_VERBOSITY_VERBOSE = "Verbose"

_LEVEL_BY_VERBOSITY: dict[str, int] = {
    LOGGING_VERBOSITY_QUIET: logging.WARNING,
    LOGGING_VERBOSITY_NORMAL: logging.INFO,
    LOGGING_VERBOSITY_VERBOSE: logging.DEBUG,
}

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILENAME = "zd_wrapper.log"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
_STARTUP_BUFFER_CAPACITY = 256

_LOCK = threading.Lock()
_startup_buffer_handler: "_BoundedStartupBufferHandler | None" = None
_active_file_handler: "GuardedRotatingFileHandler | None" = None
_managed_user_data_dir: Path | None = None
_prior_root_level_raw: int | None = None
_prior_effective_level: int | None = None
_pinned_handler_levels: list[tuple[logging.Handler, int]] = []


def normalize_logging_verbosity(value: object) -> str:
    """Return a canonical logging verbosity value, defaulting to ``Normal``."""

    if isinstance(value, str) and value in VALID_LOGGING_VERBOSITIES:
        return value
    return LOGGING_VERBOSITY_NORMAL


def _resolve_dpg_version() -> str:
    try:
        import dearpygui  # type: ignore[import-not-found]

        return str(getattr(dearpygui, "__version__", "unknown"))
    except ImportError:
        return "not-installed"


def format_startup_banner(verbosity: str) -> str:
    """Build a single sanitized startup line with no username/hostname/env."""

    run_mode = "packaged" if getattr(sys, "frozen", False) else "source"
    os_label = f"{platform.system()} {platform.release()} ({platform.machine()})"
    commit = __build_commit__ or "dev"
    return (
        f"LegendCTL startup: version={__version__} commit={commit} "
        f"run_mode={run_mode} os={os_label} python={platform.python_version()} "
        f"dearpygui={_resolve_dpg_version()} logging_verbosity={verbosity}"
    )


class _BoundedStartupBufferHandler(logging.Handler):
    """Fixed-capacity FIFO startup log buffer (evicts oldest beyond maxlen)."""

    def __init__(self, capacity: int = _STARTUP_BUFFER_CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self.buffer: collections.deque[logging.LogRecord] = collections.deque(
            maxlen=max(1, int(capacity))
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append(record)


class GuardedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that opens and rotates only inside a pinned dir."""

    def __init__(
        self,
        user_data_dir: Path,
        *,
        filename: str = LOG_FILENAME,
        maxBytes: int = DEFAULT_MAX_BYTES,
        backupCount: int = DEFAULT_BACKUP_COUNT,
        encoding: str = "utf-8",
    ) -> None:
        self._user_data_dir = Path(user_data_dir)
        self._log_dir = self._user_data_dir / "logs"
        self._filename = filename
        self._rollover_blocked = False
        self._reopen_blocked = False
        self._rollover_retry_after = 0.0
        self._closed = False
        super().__init__(
            str(self._log_dir / self._filename),
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=True,
        )
        # Admit and open eagerly so startup failures surface immediately.
        self.stream = self._open()
        self.delay = False

    @property
    def user_data_dir(self) -> Path:
        return self._user_data_dir

    def _open(self):
        with guarded_output_directory(
            self._log_dir,
            self._user_data_dir,
            allow_fallback=False,
        ) as directory:
            return directory.open_text_append(
                self._filename,
                encoding=self.encoding or "utf-8",
                buffering=1,
            )

    def shouldRollover(self, record: logging.LogRecord) -> int:
        """Check size directly on the open handle (no display-path lookups)."""

        if self._closed or self.maxBytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        msg = f"{self.format(record)}\n"
        encoded_len = len(msg.encode(self.encoding or "utf-8", errors="replace"))
        self.stream.flush()
        current_size = os.fstat(self.stream.fileno()).st_size
        if current_size > 0 and current_size + encoded_len >= self.maxBytes:
            return 1
        return 0

    def close(self) -> None:
        self.acquire()
        try:
            self._closed = True
            super().close()
        finally:
            self.release()

    def _try_reopen(self) -> None:
        if self._closed:
            return
        try:
            self.stream = self._open()
            self._reopen_blocked = False
            self._rollover_retry_after = 0.0
        except (OSError, ValueError):
            self.stream = None
            self._reopen_blocked = True
            self._rollover_retry_after = time.monotonic() + 30.0

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed:
            return
        try:
            if self._rollover_blocked:
                if time.monotonic() < self._rollover_retry_after:
                    return
                self.doRollover()
                if (
                    self._closed
                    or self._rollover_blocked
                    or self._reopen_blocked
                    or self.stream is None
                ):
                    return
            elif self._reopen_blocked:
                if time.monotonic() < self._rollover_retry_after:
                    return
                self._try_reopen()
                if self._closed or self._reopen_blocked or self.stream is None:
                    return
            if self.shouldRollover(record):
                self.doRollover()
            if (
                self._closed
                or self._rollover_blocked
                or self._reopen_blocked
                or self.stream is None
            ):
                return
            logging.FileHandler.emit(self, record)
        except Exception:  # noqa: BLE001 — standard logging.Handler contract
            self.handleError(record)

    def doRollover(self) -> None:
        if self._closed:
            return
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        try:
            # Keep every ancestor pinned with FILE_SHARE_READ and the leaf dir
            # pinned without FILE_SHARE_DELETE while performing handle-bound
            # renames (checking regular single-link non-reparse on every file).
            with guarded_output_directory(
                self._log_dir,
                self._user_data_dir,
                allow_fallback=False,
                allow_rename=True,
            ) as directory:
                active_exists = True
                try:
                    os.lstat(directory.path / self._filename)
                except FileNotFoundError:
                    active_exists = False
                if self.backupCount > 0 and active_exists:
                    # Find the lowest vacant backup slot 1..backupCount so a
                    # rollover never deletes an existing live backup (or drops
                    # history on retry after a partial failure). Only the
                    # expired tail slot (.backupCount) is unlinked, and only
                    # when every slot 1..backupCount is occupied.
                    free_idx: int | None = None
                    for idx in range(1, self.backupCount + 1):
                        try:
                            os.lstat(directory.path / f"{self._filename}.{idx}")
                        except FileNotFoundError:
                            free_idx = idx
                            break
                    if free_idx is None:
                        try:
                            directory.unlink_file(f"{self._filename}.{self.backupCount}")
                        except FileNotFoundError:
                            pass
                        free_idx = self.backupCount
                    for idx in range(free_idx - 1, 0, -1):
                        sfn = f"{self._filename}.{idx}"
                        dfn = f"{self._filename}.{idx + 1}"
                        try:
                            directory.rename_noreplace(sfn, dfn)
                        except FileNotFoundError:
                            pass
                    try:
                        directory.rename_noreplace(self._filename, f"{self._filename}.1")
                    except FileNotFoundError:
                        pass
            self._rollover_blocked = False
            self._rollover_retry_after = 0.0
        except (OSError, ValueError):
            self._rollover_blocked = True
            self._rollover_retry_after = time.monotonic() + 30.0
        if not self._closed and not self._rollover_blocked:
            self._try_reopen()


def _capture_prior_root_and_pin_handlers(root_logger: logging.Logger) -> None:
    global _prior_root_level_raw, _prior_effective_level
    if _prior_root_level_raw is not None:
        return
    _prior_root_level_raw = root_logger.level
    effective = (
        root_logger.level
        if root_logger.level != logging.NOTSET
        else root_logger.getEffectiveLevel()
    )
    _prior_effective_level = effective
    # Pin pre-existing NOTSET handlers (such as basicConfig's stderr
    # StreamHandler) to the prior effective level so lowering the root to
    # DEBUG for buffering or Verbose file logging never leaks unscrubbed
    # DEBUG lines to stderr.
    for existing in list(root_logger.handlers):
        if existing is _active_file_handler or existing is _startup_buffer_handler:
            continue
        if existing.level == logging.NOTSET:
            _pinned_handler_levels.append((existing, existing.level))
            existing.setLevel(effective)


def _restore_root_and_handlers(root_logger: logging.Logger) -> None:
    global _prior_root_level_raw, _prior_effective_level
    if _prior_root_level_raw is not None:
        root_logger.setLevel(_prior_root_level_raw)
        _prior_root_level_raw = None
        _prior_effective_level = None
    for handler, original_level in _pinned_handler_levels:
        handler.setLevel(original_level)
    _pinned_handler_levels.clear()


def _recompute_root_level(
    root_logger: logging.Logger,
    *,
    active_file_level: int | None,
) -> None:
    if active_file_level is None:
        if _prior_root_level_raw is not None:
            root_logger.setLevel(_prior_root_level_raw)
        return
    baseline = _prior_effective_level if _prior_effective_level is not None else logging.INFO
    root_logger.setLevel(min(baseline, active_file_level))


def begin_startup_logging(
    *,
    root_logger: logging.Logger | None = None,
    capacity: int = _STARTUP_BUFFER_CAPACITY,
) -> _BoundedStartupBufferHandler:
    """Buffer early startup log records until settings determine file logging."""

    global _startup_buffer_handler
    target_root = root_logger if root_logger is not None else logging.getLogger()
    with _LOCK:
        if _startup_buffer_handler is not None:
            return _startup_buffer_handler
        _capture_prior_root_and_pin_handlers(target_root)
        target_root.setLevel(logging.DEBUG)
        handler = _BoundedStartupBufferHandler(capacity=capacity)
        target_root.addHandler(handler)
        _startup_buffer_handler = handler
        return handler


def _discard_startup_buffer(root_logger: logging.Logger) -> list[logging.LogRecord]:
    global _startup_buffer_handler
    handler = _startup_buffer_handler
    if handler is None:
        return []
    records = list(handler.buffer)
    handler.buffer.clear()
    try:
        root_logger.removeHandler(handler)
    finally:
        handler.close()
    _startup_buffer_handler = None
    return records


def _detach_active_handler(root_logger: logging.Logger) -> None:
    global _active_file_handler
    handler = _active_file_handler
    if handler is None:
        return
    try:
        root_logger.removeHandler(handler)
    finally:
        try:
            handler.flush()
        except OSError:
            pass
        try:
            handler.close()
        except OSError:
            pass
    _active_file_handler = None


def apply_logging_verbosity(
    verbosity: object,
    *,
    user_data_dir: Path | None = None,
    root_logger: logging.Logger | None = None,
    emit_banner: bool = True,
) -> bool:
    """Apply ``Off`` / ``Quiet`` / ``Normal`` / ``Verbose`` to file logging.

    Returns ``True`` when the rotating file handler is attached and active,
    ``False`` when file logging is disabled (``Off``) or could not be attached.
    When ``user_data_dir`` is omitted and no startup directory has been
    registered (e.g. headless unit tests), this is a safe no-op returning
    ``False``.
    """

    global _active_file_handler, _managed_user_data_dir

    normalized = normalize_logging_verbosity(verbosity)
    target_root = root_logger if root_logger is not None else logging.getLogger()

    with _LOCK:
        if user_data_dir is not None:
            _managed_user_data_dir = Path(user_data_dir)
        resolved_dir = _managed_user_data_dir

        if (
            resolved_dir is None
            and _startup_buffer_handler is None
            and _active_file_handler is None
        ):
            # Headless unit-test path where startup never registered a data dir:
            # strict no-op without mutating logger or module state.
            return False

        _capture_prior_root_and_pin_handlers(target_root)

        if normalized == LOGGING_VERBOSITY_OFF:
            _discard_startup_buffer(target_root)
            _detach_active_handler(target_root)
            _recompute_root_level(target_root, active_file_level=None)
            return False

        if resolved_dir is None:
            _discard_startup_buffer(target_root)
            _recompute_root_level(target_root, active_file_level=None)
            return False

        target_level = _LEVEL_BY_VERBOSITY[normalized]
        buffered = _discard_startup_buffer(target_root)

        if (
            _active_file_handler is not None
            and _active_file_handler.user_data_dir == resolved_dir
        ):
            _active_file_handler.setLevel(target_level)
            handler = _active_file_handler
            freshly_attached = False
        else:
            _detach_active_handler(target_root)
            try:
                handler = GuardedRotatingFileHandler(
                    resolved_dir,
                    maxBytes=DEFAULT_MAX_BYTES,
                    backupCount=DEFAULT_BACKUP_COUNT,
                    encoding="utf-8",
                )
            except (OSError, ValueError):
                _recompute_root_level(target_root, active_file_level=None)
                logger.warning(
                    "File logger could not be attached; continuing without file logging",
                    exc_info=True,
                )
                return False
            handler.setLevel(target_level)
            handler.setFormatter(PathScrubbingFormatter(LOG_FORMAT))
            target_root.addHandler(handler)
            _active_file_handler = handler
            freshly_attached = True

        _recompute_root_level(target_root, active_file_level=target_level)

        if freshly_attached:
            if emit_banner and target_level <= logging.INFO:
                banner_record = target_root.makeRecord(
                    name=__name__,
                    level=logging.INFO,
                    fn="",
                    lno=0,
                    msg=format_startup_banner(normalized),
                    args=(),
                    exc_info=None,
                )
                handler.handle(banner_record)
            for record in buffered:
                if record.levelno >= target_level:
                    handler.handle(record)

        return True


def is_file_logging_active() -> bool:
    with _LOCK:
        return _active_file_handler is not None


def shutdown_file_logging(*, root_logger: logging.Logger | None = None) -> None:
    """Detach and close any managed file/buffer handlers (teardown & tests)."""

    global _managed_user_data_dir
    target_root = root_logger if root_logger is not None else logging.getLogger()
    with _LOCK:
        _discard_startup_buffer(target_root)
        _detach_active_handler(target_root)
        _restore_root_and_handlers(target_root)
        _managed_user_data_dir = None


__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_MAX_BYTES",
    "GuardedRotatingFileHandler",
    "LOGGING_VERBOSITY_NORMAL",
    "LOGGING_VERBOSITY_OFF",
    "LOGGING_VERBOSITY_QUIET",
    "LOGGING_VERBOSITY_VERBOSE",
    "LOG_FILENAME",
    "LOG_FORMAT",
    "VALID_LOGGING_VERBOSITIES",
    "apply_logging_verbosity",
    "begin_startup_logging",
    "format_startup_banner",
    "is_file_logging_active",
    "normalize_logging_verbosity",
    "shutdown_file_logging",
]
