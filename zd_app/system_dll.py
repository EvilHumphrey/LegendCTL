"""Load Windows system libraries without an application/bundle directory search.

PyInstaller's ctypes hook can replace even a missing absolute DLL path with a
same-named file in ``_MEIPASS``. Use CPython's native LoadLibraryExW binding,
then wrap its handle with a nameless WinDLL (which performs no second load).
This module is for OS DLLs only; bundled third-party libraries keep their own
loading policy.
"""

from __future__ import annotations

import _ctypes
import ctypes
import ntpath
from functools import lru_cache


LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_SYSTEM_DLL_NAMES = frozenset({
    "kernel32.dll", "user32.dll", "setupapi.dll", "hid.dll", "cfgmgr32.dll",
    "dbghelp.dll", "ntdll.dll", "xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll",
})
_native_load = getattr(_ctypes, "LoadLibrary", None)


def _wrap_handle(handle: int, *, use_last_error: bool = False):
    # None prevents PyInstaller's _frozen_name from rewriting the name;
    # handle prevents ctypes from invoking the Windows loader again. Each
    # caller needs its own wrapper: argtypes may use different GUID classes.
    return ctypes.WinDLL(
        None, handle=handle, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32,
        use_last_error=use_last_error,
    )


@lru_cache(maxsize=None)
def _load_native(name: str) -> int:
    # Retain native references for process lifetime, like ctypes' windll cache,
    # without sharing mutable bindings. Concurrent first misses may each load
    # a reference; the cache prevents repeated loads on subsequent requests.
    if _native_load is None:
        raise OSError("Windows system DLL loading is unavailable")
    return _native_load(name, LOAD_LIBRARY_SEARCH_SYSTEM32)


@lru_cache(maxsize=1)
def _system_directory() -> str:
    # Bootstrap only: CPython's native binding calls LoadLibraryExW directly,
    # bypassing PyInstaller. kernel32 is already loaded by CPython/Windows;
    # the SYSTEM32 flag also restricts any search/dependencies to the OS dir.
    # No environment variable, working directory or bundle path is consulted.
    kernel32 = _wrap_handle(_load_native("kernel32.dll"))
    get_directory = kernel32.GetSystemDirectoryW
    get_directory.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
    get_directory.restype = ctypes.c_uint
    size = 260
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        length = int(get_directory(buffer, size))
        if length == 0:
            raise OSError("GetSystemDirectoryW failed")
        if length >= size:
            size = length + 1
            continue
        directory = buffer.value
        drive, _ = ntpath.splitdrive(directory)
        if (
            len(directory) != length or "\x00" in directory
            or len(drive) != 2 or not drive[0].isalpha() or drive[1] != ":"
            or not ntpath.isabs(directory)
            or ".." in directory.replace("/", "\\").split("\\")
        ):
            raise OSError("GetSystemDirectoryW returned an invalid system directory")
        return directory
    raise OSError("GetSystemDirectoryW returned an oversized system directory")


def load_system_dll(name: str, *, use_last_error: bool = False):
    """Load an inventoried OS DLL by its actual absolute system-directory path.

    Missing OS DLLs raise OSError; no application, current-directory, PATH,
    SystemRoot or frozen-bundle fallback is permitted. Function bindings and
    caller-specific failure handling remain the caller's responsibility.
    """
    filename = name.lower()
    if not filename.endswith(".dll"):
        filename += ".dll"
    if filename not in _SYSTEM_DLL_NAMES:
        raise ValueError("Expected an inventoried Windows system DLL basename")
    path = ntpath.join(_system_directory(), filename)
    return _wrap_handle(_load_native(path), use_last_error=use_last_error)
