"""Small durable atomic-write primitive for local app-data records.

The helper writes a temporary file in the target's own directory, flushes and
fsyncs it, closes it, then publishes it with ``os.replace``.  Keeping
the temporary file on the target volume makes the final replace atomic on
Windows.  On any failure the temporary file is removed best-effort, so callers
never observe a partial target file.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    on_fsync_error: Callable[[Path], None] | None = None,
) -> Path:
    """Durably replace ``path`` with ``data`` without publishing partial bytes.

    The target's parent is created when necessary.  ``data`` must already be
    serialized by the caller; this primitive deliberately has no JSON or text
    policy of its own. By default fsync failures propagate. A caller with a
    best-effort fsync contract may supply ``on_fsync_error`` to log the failure
    and allow publication to continue; other write failures still propagate.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    handle = None
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                if on_fsync_error is None:
                    raise
                on_fsync_error(temp_path)
        os.replace(temp_path, target)
        return target
    except Exception:
        if fd != -1:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = ["atomic_write_bytes"]
