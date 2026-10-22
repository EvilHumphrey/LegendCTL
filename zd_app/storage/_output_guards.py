"""Local-only diagnostic output, with admission before filesystem lookup.

The returned paths are display receipts, not authority to reopen a file. Writes
use exclusive new files relative to pinned directories. Windows pins every
ancestor without write/delete sharing and uses OBJ_DONT_REPARSE for every open;
POSIX walks dirfds with O_NOFOLLOW. No resolve/stat pre-scan is a security gate.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from zd_app.storage._import_guards import (
    _IO_STATUS_BLOCK,
    _OBJECT_ATTRIBUTES,
    _UNICODE_STRING,
    _get_windows_drive_type,
)
from zd_app.system_dll import load_system_dll

_WINDOWS = os.name == "nt"
# Only direct removable/fixed disk volumes are supported. RAM disks, SUBST
# aliases and other local device mappings fail closed instead of guessing a
# native device's filesystem semantics.
_LOCAL_DRIVE_TYPES = {2, 3}
MAX_OUTPUT_PATH_UNITS = 32760
MAX_OUTPUT_COMPONENTS = 128
_DEVICE_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_LOCAL_VOLUME = re.compile(r"\\Device\\HarddiskVolume[0-9]+", re.I)


class UnsafeOutputPathError(ValueError):
    """An output path cannot be used without crossing the local boundary."""


def _require_output_length(path: str) -> None:
    units = len(path.encode("utf-16-le", errors="surrogatepass")) // 2 if _WINDOWS else len(path)
    if units > MAX_OUTPUT_PATH_UNITS:
        raise UnsafeOutputPathError("Output path exceeds the length limit")


def _bounded_output_path(path: Path) -> Path:
    """Bound the normalized walk before drive metadata or filesystem calls."""

    _require_output_length(str(path))
    if len(path.parts) - bool(path.anchor) > MAX_OUTPUT_COMPONENTS:
        raise UnsafeOutputPathError("Output path exceeds the component limit")
    return path


def expand_output_path(value: str | Path) -> str:
    """Expand environment/home references in memory, without resolving names."""

    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise UnsafeOutputPathError("Output path must be text")
    pathmod = ntpath if _WINDOWS else os.path
    return pathmod.expanduser(pathmod.expandvars(raw))


def lexical_output_path(value: str | Path) -> Path:
    """Produce an absolute local-shaped path using only lexical operations.

    This is suitable for display comparisons. Drive type, volume mapping and
    actual directory admission are deliberately separate from display.
    """

    raw = expand_output_path(value)
    _require_output_length(raw)
    windows = raw.replace("/", "\\")
    drive, tail = ntpath.splitdrive(windows)
    if (not raw or "\0" in raw or windows.startswith("\\\\")
            or windows.startswith("\\??\\") or windows.startswith("\\Device\\")):
        raise UnsafeOutputPathError("Output path must be on a local filesystem")
    if drive and (len(drive) != 2 or not drive[0].isascii()
                  or not drive[0].isalpha() or drive[1] != ":"
                  or not tail.startswith("\\")):
        raise UnsafeOutputPathError("Output path must not be drive-relative or a device namespace")
    if _WINDOWS:
        if not drive and windows.startswith("\\"):
            raise UnsafeOutputPathError("Output path must not be rooted on an implicit drive")
        # Do not let Win32 silently strip trailing spaces/dots, interpret an
        # ADS, or translate a DOS device component. Dot traversal is normalized
        # lexically and checked for containment below, never followed on disk.
        for part in tail.split("\\"):
            if part in {"", ".", ".."}:
                continue
            if (part.endswith((" ", ".")) or _DEVICE_NAME.match(part)
                    or any(c in part for c in '<>:"|?*')
                    or any(ord(c) < 32 for c in part)):
                raise UnsafeOutputPathError("Output path has an unsafe component")
        if not drive:
            cwd = lexical_output_path(os.getcwd())
            windows = ntpath.join(str(cwd), windows)
        return _bounded_output_path(Path(ntpath.normpath(windows)))
    if drive or "\\" in raw:
        raise UnsafeOutputPathError("Output path uses a foreign filesystem namespace")
    return _bounded_output_path(Path(os.path.abspath(raw)))


def _require_local_drive(path: Path) -> None:
    if _WINDOWS and _get_windows_drive_type(str(path)) not in _LOCAL_DRIVE_TYPES:
        raise UnsafeOutputPathError("Output path must use a supported local disk")


def select_output_directory(output_dir: str | Path, allowed_root: str | Path) -> tuple[Path, bool]:
    """Admit a local spelling, or choose the admitted default for an escape.

    Unsafe namespaces fail explicitly. A normal local path outside the root
    retains the existing fallback behavior. The root is checked first so an
    unsafe default can never become a fallback target.
    """

    root = lexical_output_path(allowed_root)
    requested = lexical_output_path(output_dir)
    try:
        requested.relative_to(root)
    except ValueError:
        selected, fallback = _bounded_output_path(root / "diagnostics"), True
    else:
        selected, fallback = requested, False
    _require_local_drive(root)
    _require_local_drive(requested)
    return selected, fallback


def _windows_volume(drive: str) -> str:
    """Read the local DOS mapping, then use its native disk name for I/O.

    QueryDosDevice is namespace metadata, not an open of the mapped target.
    Reject redirectors and SUBST aliases (which may themselves traverse a
    junction). RAM-disk and other non-disk device targets are unsupported even
    if their drive type reports fixed/removable. Using the captured disk
    target avoids a drive-remapping race.
    """

    kernel32 = load_system_dll("kernel32", use_last_error=True)
    query = kernel32.QueryDosDeviceW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    query.restype = ctypes.c_ulong
    buffer = ctypes.create_unicode_buffer(32768)
    if not query(drive, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    target = buffer.value
    if not _LOCAL_VOLUME.fullmatch(target):
        raise UnsafeOutputPathError("Output drive must map directly to a local disk volume")
    return target


def _windows_open(name: str, parent: int | None, *, directory: bool, create: bool) -> int:
    """Open/create relative to a pinned handle; never follow a reparse point."""

    buffer = ctypes.create_unicode_buffer(name)
    size = len(name.encode("utf-16-le"))
    if size > 65532:
        raise UnsafeOutputPathError("Output name is too long")
    unicode_name = _UNICODE_STRING(size, size + 2, ctypes.cast(buffer, ctypes.c_void_p))
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES), parent, ctypes.pointer(unicode_name),
        0x40 | 0x1000, None, None,  # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE
    )
    status_block = _IO_STATUS_BLOCK()
    handle = ctypes.c_void_p()
    ntdll = load_system_dll("ntdll", use_last_error=True)
    create_file = ntdll.NtCreateFile
    create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
        ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
    ]
    create_file.restype = ctypes.c_long
    status = create_file(
        ctypes.byref(handle),
        0x100000 | 0x80 | (0x1 | 0x20 if directory else 0x2),
        ctypes.byref(attributes), ctypes.byref(status_block), None, 0,
        0x1,  # FILE_SHARE_READ only: pin names against writes/reparse/delete
        (3 if create else 1) if directory else 2,  # OPEN_IF / OPEN / CREATE
        0x20 | (0x1 if directory else 0x40), None, 0,
    )
    if status < 0:
        if status & 0xFFFFFFFF == 0xC000050B:
            raise UnsafeOutputPathError("Output path must not traverse a symlink or junction")
        to_error = ntdll.RtlNtStatusToDosError
        to_error.argtypes = [ctypes.c_long]
        to_error.restype = ctypes.c_ulong
        raise ctypes.WinError(to_error(status))
    if not handle.value:
        raise OSError("Output open returned no handle")
    return int(handle.value)


def _close_windows_handle(handle: int) -> None:
    close = load_system_dll("kernel32").CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    close(handle)


class GuardedOutputDirectory:
    """A scoped output capability; its display path must not be used for writes."""

    def __init__(self, path: Path, handle: int, used_fallback: bool):
        self.path = path
        self.used_fallback = used_fallback
        self._handle: int | None = handle

    def write_text(self, filename: str, text: str, *, rename_on_collision: bool = True) -> Path:
        """Create a new file; paired exports can opt out of collision renaming."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        if (not filename or filename in {".", ".."}
                or any(c in filename for c in '/\\\0:<>"|?*')
                or filename.endswith((" ", ".")) or _DEVICE_NAME.match(filename)
                or any(ord(c) < 32 for c in filename)):
            raise UnsafeOutputPathError("Output name must be a regular leaf filename")
        for _attempt in range(5):
            _bounded_output_path(self.path / filename)
            try:
                descriptor = self._create_file(filename)
                break
            except FileExistsError:
                if not rename_on_collision:
                    raise
                original = Path(filename)
                filename = f"{original.stem}_{uuid4().hex}{original.suffix}"
        else:
            raise FileExistsError("Unable to allocate a new output filename")
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(text)
        return self.path / filename

    def _create_file(self, filename: str) -> int:
        if _WINDOWS:
            import msvcrt

            handle = _windows_open(filename, self._handle, directory=False, create=True)
            try:
                return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            except BaseException:
                _close_windows_handle(handle)
                raise
        return os.open(
            filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=self._handle,
        )


@contextmanager
def guarded_output_directory(
    output_dir: str | Path, allowed_root: str | Path,
) -> Iterator[GuardedOutputDirectory]:
    """Admit, pin and create local directories; keep pins through use.

    Shell opening may be dispatched while this context is alive. Windows pins
    protect that dispatch, not Explorer's asynchronous lifetime after return.
    """

    path, fallback = select_output_directory(output_dir, allowed_root)
    with ExitStack() as stack:
        if _WINDOWS:
            volume = _windows_volume(path.drive)
            handle = _windows_open(volume + "\\", None, directory=True, create=False)
            stack.callback(_close_windows_handle, handle)
            for part in path.parts[1:]:
                handle = _windows_open(part, handle, directory=True, create=True)
                stack.callback(_close_windows_handle, handle)
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            handle = os.open(path.anchor, flags)
            stack.callback(os.close, handle)
            for part in path.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=handle)
                except FileExistsError:
                    pass
                handle = os.open(part, flags, dir_fd=handle)
                stack.callback(os.close, handle)
        directory = GuardedOutputDirectory(path, handle, fallback)
        try:
            yield directory
        finally:
            directory._handle = None
