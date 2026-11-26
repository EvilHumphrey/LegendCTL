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
import stat
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


def select_output_directory(
    output_dir: str | Path,
    allowed_root: str | Path,
    *,
    allow_fallback: bool = True,
) -> tuple[Path, bool]:
    """Admit a local spelling, or choose the admitted default for an escape.

    Unsafe namespaces fail explicitly. When ``allow_fallback`` is True, a normal
    local path outside the root retains the existing fallback behavior; when
    False, an out-of-root path raises :class:`UnsafeOutputPathError`. The root
    is checked first so an unsafe default can never become a fallback target.
    """

    root = lexical_output_path(allowed_root)
    requested = lexical_output_path(output_dir)
    try:
        requested.relative_to(root)
    except ValueError:
        if not allow_fallback:
            raise UnsafeOutputPathError("Output path escapes the allowed root directory") from None
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


def _windows_open(
    name: str,
    parent: int | None,
    *,
    directory: bool,
    create: bool,
    append: bool = False,
    access: int | None = None,
    share: int = 0x1,
) -> int:
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
    if access is None:
        if directory:
            access = 0x100000 | 0x80 | 0x1 | 0x20
        elif append:
            # SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_APPEND_DATA
            access = 0x100000 | 0x80 | 0x4
        else:
            access = 0x100000 | 0x80 | 0x2
    status = create_file(
        ctypes.byref(handle),
        access,
        ctypes.byref(attributes), ctypes.byref(status_block), None, 0,
        share,  # Default FILE_SHARE_READ only: pin names against writes/reparse/delete
        (3 if create else 1) if directory else (2 if create else 1),
        # directories: OPEN_IF / OPEN; files: CREATE / OPEN
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


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_ulong),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("nNumberOfLinks", ctypes.c_ulong),
        ("nFileIndexHigh", ctypes.c_ulong),
        ("nFileIndexLow", ctypes.c_ulong),
    ]


def _windows_assert_regular_single_link(handle: int) -> None:
    kernel32 = load_system_dll("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_info.restype = ctypes.c_int
    info = _BY_HANDLE_FILE_INFORMATION()
    if not get_info(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.dwFileAttributes & (0x10 | 0x400) or info.nNumberOfLinks != 1:
        raise UnsafeOutputPathError(
            "Output file must be regular and have exactly one link"
        )


def _windows_ntstatus_to_error(status: int) -> OSError:
    if status & 0xFFFFFFFF == 0xC000050B:
        return UnsafeOutputPathError("Output path must not traverse a symlink or junction")
    ntdll = load_system_dll("ntdll", use_last_error=True)
    to_error = ntdll.RtlNtStatusToDosError
    to_error.argtypes = [ctypes.c_long]
    to_error.restype = ctypes.c_ulong
    return ctypes.WinError(to_error(status))


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
        self._validate_filename(filename)
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

    def open_text_truncate(self, filename: str, *, buffering: int = 1):
        """Open one pinned ordinary file for a guarded truncate-and-rewrite.

        The directory capability remains live for the returned stream's
        lifetime. Existing reparses, non-regular files and hardlink aliases are
        refused before truncation. Callers own and must close the stream.
        """

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        descriptor = self._open_file_truncate(filename)
        try:
            return os.fdopen(
                descriptor, "w", encoding="utf-8", newline="", buffering=buffering
            )
        except BaseException:
            os.close(descriptor)
            raise

    def open_binary_create(self, filename: str, *, rename_on_collision: bool = True):
        """Create a new binary file relative to the pinned directory.

        Returns ``(stream, display_path)``. Callers own and must close the stream.
        """

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
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
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        return stream, self.path / filename

    def open_binary_truncate(self, filename: str):
        """Open one pinned ordinary file for a guarded binary truncate-and-write."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        descriptor = self._open_file_truncate(filename)
        try:
            return os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise

    def open_binary_append(self, filename: str):
        """Open/create one pinned ordinary regular single-link file for binary append."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        descriptor = self._open_file_append(filename)
        try:
            return os.fdopen(descriptor, "ab")
        except BaseException:
            os.close(descriptor)
            raise

    def open_text_append(
        self,
        filename: str,
        *,
        encoding: str = "utf-8",
        buffering: int = 1,
    ):
        """Open/create one pinned ordinary regular single-link file for text append."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        descriptor = self._open_file_append(filename)
        try:
            return os.fdopen(
                descriptor, "a", encoding=encoding, newline="", buffering=buffering
            )
        except BaseException:
            os.close(descriptor)
            raise

    def open_binary_read(self, filename: str):
        """Open one pinned ordinary regular single-link file for binary read."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        descriptor = self._open_file_read(filename)
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def rename_noreplace(self, src_name: str, dst_name: str) -> None:
        """Atomically rename ``src_name`` to absent ``dst_name`` inside this pinned dir.

        ``src_name`` must be a regular single-link non-reparse file, and
        ``dst_name`` must not already exist (fail closed with
        :class:`FileExistsError` without deleting or overwriting ``dst_name``).
        On Windows, requires ``allow_rename=True`` on the enclosing
        :func:`guarded_output_directory` context.
        """

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(src_name)
        self._validate_filename(dst_name)
        _bounded_output_path(self.path / src_name)
        _bounded_output_path(self.path / dst_name)
        if src_name == dst_name:
            raise FileExistsError(dst_name)
        if _WINDOWS:
            # Verify src_handle (no FILE_SHARE_DELETE, OBJ_DONT_REPARSE,
            # regular single-link) and rename with Flags=0 (ReplaceIfExists=False)
            # so an existing or racing dst_name can never be overwritten or lost.
            src_handle = _windows_open(
                src_name,
                self._handle,
                directory=False,
                create=False,
                access=0x100000 | 0x10000 | 0x80,  # SYNCHRONIZE | DELETE | FILE_READ_ATTRIBUTES
                share=0x1,
            )
            try:
                _windows_assert_regular_single_link(src_handle)
                dst_bytes = dst_name.encode("utf-16-le")

                class _RENAME_INFO_EX(ctypes.Structure):
                    _fields_ = [
                        ("Flags", ctypes.c_ulong),
                        ("RootDirectory", ctypes.c_void_p),
                        ("FileNameLength", ctypes.c_ulong),
                        ("FileName", ctypes.c_ubyte * len(dst_bytes)),
                    ]

                info = _RENAME_INFO_EX()
                info.Flags = 0  # ReplaceIfExists=False: fail closed if dst_name exists
                info.RootDirectory = self._handle
                info.FileNameLength = len(dst_bytes)
                ctypes.memmove(ctypes.addressof(info.FileName), dst_bytes, len(dst_bytes))
                ntdll = load_system_dll("ntdll", use_last_error=True)
                set_info = ntdll.NtSetInformationFile
                set_info.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_IO_STATUS_BLOCK),
                    ctypes.c_void_p,
                    ctypes.c_ulong,
                    ctypes.c_ulong,
                ]
                set_info.restype = ctypes.c_long
                iosb = _IO_STATUS_BLOCK()
                status = set_info(
                    src_handle, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info), 65
                )
                if status < 0 and (status & 0xFFFFFFFF) in (
                    0xC0000003,
                    0xC000000D,
                    0xC0000010,
                    0xC00000BB,
                ):
                    class _RENAME_INFO(ctypes.Structure):
                        _fields_ = [
                            ("ReplaceIfExists", ctypes.c_ubyte),
                            ("RootDirectory", ctypes.c_void_p),
                            ("FileNameLength", ctypes.c_ulong),
                            ("FileName", ctypes.c_ubyte * len(dst_bytes)),
                        ]

                    legacy_info = _RENAME_INFO()
                    legacy_info.ReplaceIfExists = 0
                    legacy_info.RootDirectory = self._handle
                    legacy_info.FileNameLength = len(dst_bytes)
                    ctypes.memmove(
                        ctypes.addressof(legacy_info.FileName), dst_bytes, len(dst_bytes)
                    )
                    status = set_info(
                        src_handle,
                        ctypes.byref(iosb),
                        ctypes.byref(legacy_info),
                        ctypes.sizeof(legacy_info),
                        10,
                    )
                if status < 0:
                    raise _windows_ntstatus_to_error(status)
            finally:
                _close_windows_handle(src_handle)
            return

        src_fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._handle)
        try:
            src_info = os.fstat(src_fd)
            if not stat.S_ISREG(src_info.st_mode) or src_info.st_nlink != 1:
                raise UnsafeOutputPathError(
                    "Output file must be regular and have exactly one link"
                )
        finally:
            os.close(src_fd)
        # Non-overwriting POSIX link+unlink; a crash between steps leaves st_nlink==2,
        # which fails closed on the next single-link check without losing either file.
        link_kwargs: dict[str, object] = {
            "src_dir_fd": self._handle,
            "dst_dir_fd": self._handle,
        }
        if os.link in os.supports_follow_symlinks:
            link_kwargs["follow_symlinks"] = False
        os.link(src_name, dst_name, **link_kwargs)
        try:
            os.unlink(src_name, dir_fd=self._handle)
        except BaseException:
            try:
                dst_info = os.lstat(dst_name, dir_fd=self._handle)
                if (dst_info.st_dev, dst_info.st_ino) == (
                    src_info.st_dev,
                    src_info.st_ino,
                ):
                    os.unlink(dst_name, dir_fd=self._handle)
            except OSError:
                pass
            raise

    def unlink_file(self, filename: str) -> None:
        """Delete one pinned regular single-link non-reparse file inside this dir."""

        if self._handle is None:
            raise ValueError("Output directory is closed")
        self._validate_filename(filename)
        _bounded_output_path(self.path / filename)
        if _WINDOWS:
            fh = _windows_open(
                filename,
                self._handle,
                directory=False,
                create=False,
                access=0x100000 | 0x10000 | 0x80,  # SYNCHRONIZE | DELETE | FILE_READ_ATTRIBUTES
                share=0x1,
            )
            try:
                _windows_assert_regular_single_link(fh)
                ntdll = load_system_dll("ntdll", use_last_error=True)
                set_info = ntdll.NtSetInformationFile
                set_info.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_IO_STATUS_BLOCK),
                    ctypes.c_void_p,
                    ctypes.c_ulong,
                    ctypes.c_ulong,
                ]
                set_info.restype = ctypes.c_long
                # Prefer POSIX delete semantics (FileDispositionInformationEx=64)
                # so the directory entry disappears immediately on close; fall
                # back to FileDispositionInformation=13 only if unsupported.
                disp_ex = ctypes.c_ulong(0x1 | 0x2)  # DELETE | POSIX_SEMANTICS
                iosb = _IO_STATUS_BLOCK()
                status = set_info(fh, ctypes.byref(iosb), ctypes.byref(disp_ex), ctypes.sizeof(disp_ex), 64)
                if status < 0 and (status & 0xFFFFFFFF) in (
                    0xC0000003,
                    0xC000000D,
                    0xC0000010,
                    0xC00000BB,
                ):
                    disp = ctypes.c_ubyte(1)
                    status = set_info(fh, ctypes.byref(iosb), ctypes.byref(disp), 1, 13)
                if status < 0:
                    raise _windows_ntstatus_to_error(status)
            finally:
                _close_windows_handle(fh)
            return

        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._handle)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeOutputPathError(
                    "Output file must be regular and have exactly one link"
                )
        finally:
            os.close(fd)
        os.unlink(filename, dir_fd=self._handle)

    def _open_file_read(self, filename: str) -> int:
        if _WINDOWS:
            import msvcrt

            handle = _windows_open(
                filename,
                self._handle,
                directory=False,
                create=False,
                access=0x100000 | 0x80 | 0x1,  # SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_READ_DATA
                share=0x1 | 0x2,
            )
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _close_windows_handle(handle)
                raise
        else:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._handle,
            )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeOutputPathError(
                    "Output file must be regular and have exactly one link"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _validate_filename(filename: str) -> None:
        if (not filename or filename in {".", ".."}
                or any(c in filename for c in '/\\\0:<>"|?*')
                or filename.endswith((" ", ".")) or _DEVICE_NAME.match(filename)
                or any(ord(c) < 32 for c in filename)):
            raise UnsafeOutputPathError("Output name must be a regular leaf filename")

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

    def _open_file_truncate(self, filename: str) -> int:
        if _WINDOWS:
            import msvcrt

            try:
                handle = _windows_open(filename, self._handle, directory=False, create=True)
            except FileExistsError:
                handle = _windows_open(filename, self._handle, directory=False, create=False)
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            except BaseException:
                _close_windows_handle(handle)
                raise
        else:
            descriptor = os.open(
                filename, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                0o600, dir_fd=self._handle,
            )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeOutputPathError(
                    "Output file must be regular and have exactly one link"
                )
            os.ftruncate(descriptor, 0)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_file_append(self, filename: str) -> int:
        if _WINDOWS:
            import msvcrt

            try:
                handle = _windows_open(
                    filename, self._handle, directory=False, create=True, append=True
                )
            except FileExistsError:
                handle = _windows_open(
                    filename, self._handle, directory=False, create=False, append=True
                )
            try:
                descriptor = msvcrt.open_osfhandle(
                    handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY
                )
            except BaseException:
                _close_windows_handle(handle)
                raise
        else:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._handle,
            )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeOutputPathError(
                    "Output file must be regular and have exactly one link"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


@contextmanager
def guarded_output_directory(
    output_dir: str | Path,
    allowed_root: str | Path,
    *,
    allow_fallback: bool = True,
    allow_rename: bool = False,
) -> Iterator[GuardedOutputDirectory]:
    """Admit, pin and create local directories; keep pins through use.

    Shell opening may be dispatched while this context is alive. Windows pins
    protect that dispatch, not Explorer's asynchronous lifetime after return.
    When ``allow_rename`` is True on Windows, every ancestor remains pinned
    with ``FILE_SHARE_READ`` only while the leaf directory allows
    ``FILE_SHARE_READ | FILE_SHARE_WRITE`` (still denying ``FILE_SHARE_DELETE``)
    so handle-bound :meth:`GuardedOutputDirectory.rename_noreplace` can rename
    children inside the pinned directory.
    """

    if allow_fallback:
        path, fallback = select_output_directory(output_dir, allowed_root)
    else:
        path, fallback = select_output_directory(
            output_dir, allowed_root, allow_fallback=False
        )
    with ExitStack() as stack:
        if _WINDOWS:
            volume = _windows_volume(path.drive)
            handle = _windows_open(volume + "\\", None, directory=True, create=False)
            stack.callback(_close_windows_handle, handle)
            parts = path.parts[1:]
            for idx, part in enumerate(parts):
                share = 0x3 if (allow_rename and idx == len(parts) - 1) else 0x1
                handle = _windows_open(
                    part, handle, directory=True, create=True, share=share
                )
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
