"""Shared test-package init: stabilize Dear PyGui's context lifecycle.

``python -m unittest discover -s tests`` runs ~140 modules in one process,
and many screen/shell tests follow the per-test pattern::

    dpg.create_context()
    try:
        # build widgets and assert
    finally:
        dpg.destroy_context()

Each create/destroy pair is clean in isolation, but the cumulative cycles
across discover exhaust DPG's native state -- every test runs and passes,
then the process segfaults at interpreter teardown before the "OK" summary
prints. Per-module runs (one cycle per process) were unaffected, which
masked the root cause. The same pre-existing failure is referenced by the
atexit comment in ``zd_app/services/xinput_poll_service.py``.

This shim collapses the per-test cycles into one shared context per process:

* The first ``dpg.create_context()`` really creates the native context.
  Subsequent calls are no-ops.
* ``dpg.destroy_context()`` is replaced with a state-clearing function that
  deletes every root item (windows, themes, font / value / handler
  registries). Children and aliases cascade, leaving an empty registry
  indistinguishable from a fresh context. The next test starts clean.
* The native context is never torn down; the OS reclaims the DLL at exit
  without DPG entering the accumulated-state teardown path.

Per-module unittest runs go through the same shim and behave identically.
"""

from __future__ import annotations

import dearpygui.dearpygui as _dpg


_real_create_context = _dpg.create_context
_context_initialized = False


def _shared_create_context() -> None:
    global _context_initialized
    if _context_initialized:
        return
    _real_create_context()
    _context_initialized = True


def _clearing_destroy_context() -> None:
    if not _context_initialized:
        return
    # get_windows() returns every root item (windows, themes, font / value /
    # handler registries). Deleting a root cascades to children and auto-
    # removes their aliases. The try/except absorbs the rare "already gone"
    # SystemError when a parent cascade races the explicit delete.
    for item in list(_dpg.get_windows()):
        try:
            _dpg.delete_item(item)
        except SystemError:
            pass


_dpg.create_context = _shared_create_context
_dpg.destroy_context = _clearing_destroy_context
