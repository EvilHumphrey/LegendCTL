"""Shared guards for importing untrusted profile / config JSON.

Imported files are untrusted: the v1 "Import Config" button and the v2 Safe
Import flow both read a user-chosen path. Reject implausibly large or deeply
nested files *before* parsing so a hostile file can neither exhaust memory nor
blow the JSON recursion limit. A real profile is a few KB and ~4 levels deep;
these are generous ceilings, not tuning knobs.

Single source of truth: both ``profile_store`` (v1) and
``wrapper_profile_store`` (v2 Safe Import) import these.
"""

from __future__ import annotations

import ctypes
import json
import ntpath
import os
import stat
import struct
from pathlib import Path
from typing import Any

MAX_IMPORT_BYTES = 1 * 1024 * 1024
MAX_IMPORT_JSON_DEPTH = 64
MAX_DIRECTORY_ENTRIES = 4096
MAX_DIRECTORY_NAME_BYTES = 1 * 1024 * 1024
_DRIVE_REMOTE = 4
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
_FILE_READ_DATA = 0x00000001
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_OPEN = 0x00000001
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_NAMES_INFORMATION = 12
_STATUS_NO_MORE_FILES = 0x80000006


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK_VALUE(ctypes.Union):
    _fields_ = [("Status", ctypes.c_long), ("Pointer", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("Value",)
    _fields_ = [
        ("Value", _IO_STATUS_BLOCK_VALUE),
        ("Information", ctypes.c_size_t),
    ]


class ImportFileTooLargeError(ValueError):
    """Raised when an import exceeds its byte ceiling."""


class UnsafeImportPathError(ValueError):
    """Raised when a user-selected import path is not a local filesystem path."""


class DirectoryListingTooLargeError(ValueError):
    """Raised instead of returning a partial directory listing."""


def _get_windows_drive_type(path: str) -> int | None:
    """Return ``GetDriveTypeW`` for a drive-letter path, when available.

    This check is metadata-only: unlike resolving, opening, or statting a path,
    it does not contact the file server behind a mapped drive.
    """

    if os.name != "nt":
        return None
    drive, _tail = ntpath.splitdrive(path)
    if len(drive) != 2 or drive[1] != ":":
        return None
    return int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\"))  # type: ignore[attr-defined]


def _open_windows_local_no_reparse(
    path: str, *, directory: bool = False, require_single_link: bool = False
) -> ctypes.c_void_p:
    """Atomically open a local Windows file or directory without reparses.

    This is deliberately the first filesystem operation after lexical/device
    checks: a component-by-component pre-scan would itself be vulnerable to a
    parent being swapped before the next component lookup. Security is enforced
    by ``NtCreateFile`` with ``OBJECT_ATTRIBUTES.Attributes`` containing
    ``OBJ_DONT_REPARSE`` (0x1000). Per Microsoft's ``OBJECT_ATTRIBUTES``
    contract, name parsing follows no reparse point and fails with
    ``STATUS_REPARSE_POINT_ENCOUNTERED`` (0xC000050B) if it sees one. This is
    stronger than ``FILE_OPEN_REPARSE_POINT``, which only controls processing
    of the final component. ``FILE_OPEN`` plus
    ``FILE_NON_DIRECTORY_FILE`` or ``FILE_DIRECTORY_FILE`` selects the object
    type, with ``FILE_SYNCHRONOUS_IO_NONALERT`` for synchronous I/O. The caller
    owns the native handle and must close it or transfer ownership to the CRT.

    Single-link reads also deny write/delete sharing for the lifetime of this
    handle and check its link count before and after reading. This blocks
    conflicting writer opens/removal of the opened name and detects persistent
    added aliases. Other link names may still change; the checks do not prove
    historical namespace ownership or the contents' provenance.

    Microsoft references:
    https://learn.microsoft.com/windows/win32/api/ntdef/ns-ntdef-_object_attributes
    https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntcreatefile
    """

    absolute = ntpath.normpath(ntpath.abspath(path))
    nt_path = f"\\??\\{absolute}"
    path_buffer = ctypes.create_unicode_buffer(nt_path)
    encoded_length = len(nt_path.encode("utf-16-le"))
    object_name = _UNICODE_STRING(
        Length=encoded_length,
        MaximumLength=encoded_length + ctypes.sizeof(ctypes.c_wchar),
        Buffer=ctypes.cast(path_buffer, ctypes.c_void_p),
    )
    object_attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=None,
        ObjectName=ctypes.pointer(object_name),
        Attributes=_OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _IO_STATUS_BLOCK()
    native_handle = ctypes.c_void_p()

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)  # type: ignore[attr-defined]
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    nt_create_file.restype = ctypes.c_long
    status = nt_create_file(
        ctypes.byref(native_handle),
        (_FILE_LIST_DIRECTORY if directory else _FILE_READ_DATA) | _SYNCHRONIZE | (
            _FILE_READ_ATTRIBUTES if require_single_link else 0
        ),
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        0,
        (
            _FILE_SHARE_READ if require_single_link
            else _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
        ),
        _FILE_OPEN,
        _FILE_SYNCHRONOUS_IO_NONALERT | (
            _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
        ),
        None,
        0,
    )
    if status < 0:
        unsigned_status = ctypes.c_ulong(status).value
        if unsigned_status == _STATUS_REPARSE_POINT_ENCOUNTERED:
            raise UnsafeImportPathError(
                "Import path must not traverse a symlink or junction"
            )
        rtl_status_to_error = ntdll.RtlNtStatusToDosError
        rtl_status_to_error.argtypes = [ctypes.c_long]
        rtl_status_to_error.restype = ctypes.c_ulong
        raise ctypes.WinError(rtl_status_to_error(status))  # type: ignore[attr-defined]

    if native_handle.value is None:
        raise OSError("NtCreateFile succeeded without returning a file handle")
    return native_handle


def _read_windows_local_no_reparse(
    path: str, max_bytes: int, *, require_single_link: bool = False
) -> bytes:
    """Read from the no-reparse handle; preserve the opt-in alias checks."""

    import msvcrt

    native_handle = _open_windows_local_no_reparse(
        path, require_single_link=require_single_link
    )

    try:
        descriptor = msvcrt.open_osfhandle(
            int(native_handle.value), os.O_RDONLY | os.O_BINARY
        )
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(native_handle)  # type: ignore[attr-defined]
        raise
    try:
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        if require_single_link:
            _require_single_link_regular_file(handle.fileno())
        payload = handle.read(max_bytes + 1)
        if require_single_link:
            _require_single_link_regular_file(handle.fileno())
        return payload


def _query_windows_directory_names(
    native_handle: ctypes.c_void_p, max_entries: int
) -> list[str]:
    """Enumerate the opened object, never its replaceable pathname.

    Request one FILE_NAMES_INFORMATION entry per synchronous query. This
    bounds calls as well as memory, without needing unbounded buffer growth or
    treating a truncated/overflow result as a complete listing.

    https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntquerydirectoryfile
    https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_names_information
    """

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)  # type: ignore[attr-defined]
    query = ntdll.NtQueryDirectoryFile
    query.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(_IO_STATUS_BLOCK), ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_ubyte, ctypes.POINTER(_UNICODE_STRING),
        ctypes.c_ubyte,
    ]
    query.restype = ctypes.c_long
    buffer = ctypes.create_string_buffer(64 * 1024)
    names: list[str] = []
    name_bytes = 0
    # Allow "." and "..", plus the query proving exhaustion (or overflow).
    for index in range(max_entries + 3):
        io_status = _IO_STATUS_BLOCK()
        status = query(
            native_handle, None, None, None, ctypes.byref(io_status),
            buffer, len(buffer), _FILE_NAMES_INFORMATION, True, None, index == 0,
        )
        if ctypes.c_ulong(status).value == _STATUS_NO_MORE_FILES:
            return names
        if status != 0:
            raise OSError(f"Directory query failed (NTSTATUS {status & 0xffffffff:#x})")
        size = io_status.Information
        if not 12 <= size <= len(buffer):
            raise OSError("Directory query returned an incomplete entry")
        next_offset, _file_index, name_length = struct.unpack_from("<III", buffer)
        if next_offset or name_length == 0 or name_length % 2 or name_length > size - 12:
            raise OSError("Directory query returned an invalid filename entry")
        name = buffer[12:12 + name_length].decode("utf-16-le", errors="surrogatepass")
        if name in {".", ".."}:
            continue
        if any(char in name for char in ("/", "\\", "\0", ":")):
            raise UnsafeImportPathError("Directory entry must be a leaf filename")
        name_bytes += name_length
        if len(names) >= max_entries or name_bytes > MAX_DIRECTORY_NAME_BYTES:
            raise DirectoryListingTooLargeError("Directory listing exceeds its limit")
        names.append(name)
    raise DirectoryListingTooLargeError("Directory listing exceeds its query limit")


def _list_posix_directory_names(path: str, max_entries: int) -> list[str]:
    """Pin each ancestor without following links, then enumerate the final fd."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = Path(path).parts
    descriptor = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        names = []
        name_bytes = 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name_bytes += len(os.fsencode(entry.name))
                if len(names) >= max_entries or name_bytes > MAX_DIRECTORY_NAME_BYTES:
                    raise DirectoryListingTooLargeError("Directory listing exceeds its limit")
                names.append(entry.name)
        return names
    finally:
        os.close(descriptor)


def list_guarded_directory(
    path: str | Path, *, max_entries: int = MAX_DIRECTORY_ENTRIES
) -> list[Path]:
    """Return bounded local child paths without traversing directory reparses.

    Windows opens the root with OBJ_DONT_REPARSE before any enumeration and
    queries that same handle. A later root/ancestor path substitution cannot
    redirect discovery. The returned paths are names, not trusted file handles:
    callers still need guarded reads. Overflow rejects the entire listing.
    """

    if max_entries < 0:
        raise ValueError("Directory entry limit must be nonnegative")
    checked_path = require_local_import_path(path)
    if os.name == "nt":
        native_handle = _open_windows_local_no_reparse(checked_path, directory=True)
        try:
            names = _query_windows_directory_names(native_handle, max_entries)
        finally:
            ctypes.windll.kernel32.CloseHandle(native_handle)  # type: ignore[attr-defined]
    else:
        names = _list_posix_directory_names(checked_path, max_entries)
    return [Path(checked_path) / name for name in names]


def _require_single_link_regular_file(descriptor: int) -> None:
    """Check the opened object, rather than a replaceable pathname."""

    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeImportPathError("File must be regular and have exactly one link")


def require_local_import_path(path: str | Path) -> str:
    """Reject network and Windows device paths before any filesystem access.

    User-selected imports are local-only. In particular, probing a UNC path or
    a mapped network drive can disclose Windows credentials before JSON
    validation gets a chance to reject the payload. App-owned readers do not
    call this guard so existing local-storage configuration remains compatible.
    """

    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise UnsafeImportPathError("Import path must be text")
    windows_path = raw_path.replace("/", "\\")
    if windows_path.startswith("\\\\") or windows_path.startswith("\\??\\"):
        raise UnsafeImportPathError("Import path must be on a local filesystem")
    if os.name != "nt":
        return os.path.abspath(raw_path)

    # The file chooser is editable, so relative input is valid. Bind the
    # mapped-drive check and the native open to one lexical absolute path:
    # checking the raw relative spelling and resolving it later could miss a
    # remote current drive (for example, incoming.json while CWD is Z:).
    checked_path = ntpath.normpath(ntpath.abspath(windows_path))
    if checked_path.startswith("\\\\") or checked_path.startswith("\\??\\"):
        raise UnsafeImportPathError("Import path must be on a local filesystem")
    if _get_windows_drive_type(checked_path) == _DRIVE_REMOTE:
        raise UnsafeImportPathError("Import path must not use a mapped network drive")
    return checked_path


def _max_json_depth(text: str) -> int:
    """Largest structural bracket-nesting depth in ``text``.

    Brackets inside JSON strings are ignored. Used to reject pathologically
    nested input *before* ``json.loads`` can recurse deep enough to raise
    ``RecursionError`` (which the import callers do not catch).
    """

    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch == "}" or ch == "]":
            depth -= 1
    return max_depth


def read_guarded_text(
    path: str | Path,
    *,
    max_bytes: int = MAX_IMPORT_BYTES,
    local_only: bool = False,
    require_single_link: bool = False,
) -> str:
    """Read at most ``max_bytes + 1`` bytes from one file handle.

    Reading and enforcing the limit on the same handle avoids the former
    stat-then-read race, where a file could be replaced or grown after its size
    check. ``local_only`` is explicit because app-owned readers may live in a
    user-configured location while user-selected import sources must not be
    network paths.

    ``require_single_link`` opts into rejecting aliases via the opened file's
    link count. It implies the local-path policy and, on Windows, uses the
    no-reparse native open with read-only sharing until the read completes.
    This does not establish provenance of the file's contents.
    """

    checked_path = os.fspath(path)
    if local_only or require_single_link:
        checked_path = require_local_import_path(path)
    if (local_only or require_single_link) and os.name == "nt":
        payload = _read_windows_local_no_reparse(
            checked_path, max_bytes, require_single_link=require_single_link
        )
    else:
        source = Path(checked_path)
        with source.open("rb") as handle:
            if require_single_link:
                _require_single_link_regular_file(handle.fileno())
            payload = handle.read(max_bytes + 1)
            if require_single_link:
                _require_single_link_regular_file(handle.fileno())
    if len(payload) > max_bytes:
        raise ImportFileTooLargeError(
            f"Import file is too large (limit {max_bytes} bytes): {path}"
        )
    return payload.decode("utf-8")


def read_guarded_json(
    path: str | Path,
    *,
    max_bytes: int = MAX_IMPORT_BYTES,
    local_only: bool = False,
    require_single_link: bool = False,
) -> Any:
    """Read and parse a JSON file behind the size and depth guards.

    Raises ``ValueError`` if the file exceeds ``max_bytes`` or nests deeper than
    ``MAX_IMPORT_JSON_DEPTH``. The caller validates the parsed value's shape
    (e.g. rejecting a non-object root).

    ``max_bytes`` defaults to ``MAX_IMPORT_BYTES`` — the right ceiling for an
    *untrusted* imported file. A trusted, app-owned, append-only store (e.g. the
    module passport, which grows one fingerprint per characterization run) can
    legitimately exceed 1 MiB over its lifetime, so those readers pass a higher
    ceiling. The depth guard is not relaxed: deep nesting is pathological for
    every JSON the wrapper reads, trusted or not, and still protects
    ``json.loads`` from ``RecursionError``.
    """

    raw = read_guarded_text(
        path, max_bytes=max_bytes, local_only=local_only,
        require_single_link=require_single_link,
    )
    if _max_json_depth(raw) > MAX_IMPORT_JSON_DEPTH:
        raise ValueError(f"Import file JSON nesting is too deep: {path}")
    return json.loads(raw)
