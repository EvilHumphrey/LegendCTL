"""Installation-scoped equality digests used only by Last Applied records.

This module is intentionally small and local to the public Last Applied
feature.  It performs no device enumeration or I/O: callers supply the stable
identifier already exposed by ``DeviceService``.  Raw identifiers are used
only as HMAC input and are never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
from enum import Enum
from pathlib import Path
from typing import Callable


STABLE_IDENTIFIER_DIGEST_VERSION = "stable_identifier_hmac_v1"
KEY_FILENAME = "controller_identity_key_v1.bin"
_KEY_BYTES = 32
_SCOPE_DOMAIN = b"legendctl.instance.scope.v1"
_STABLE_IDENTIFIER_DOMAIN = b"legendctl.last_applied.stable_identifier.v1\0"


logger = logging.getLogger(__name__)


class DigestComparison(str, Enum):
    """Three-valued comparison for installation-scoped identity evidence."""

    SAME = "same"
    DIFFERENT = "different"
    NOT_COMPARABLE = "not_comparable"


class LastAppliedIdentity:
    """Load the local key and derive LastApplied-specific stable-ID HMACs."""

    def __init__(
        self,
        app_data_root: str | Path,
        *,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._app_data_root = Path(app_data_root)
        self._token_bytes = token_bytes

    @property
    def key_path(self) -> Path:
        return self._app_data_root / KEY_FILENAME

    def binding(self, stable_identifier: str) -> tuple[str, str]:
        """Return ``(full_digest, scope_id)`` for a normalized stable ID."""

        normalized = stable_identifier.strip().casefold()
        if not normalized:
            raise ValueError("stable_identifier must be non-empty")
        key = self._load_or_create_key()
        digest = hmac.new(
            key,
            _STABLE_IDENTIFIER_DOMAIN + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        scope_id = hmac.new(key, _SCOPE_DOMAIN, hashlib.sha256).hexdigest()[:12]
        return digest, scope_id

    def _load_or_create_key(self) -> bytes:
        try:
            existing = self.key_path.read_bytes()
        except FileNotFoundError:
            existing = None
        if isinstance(existing, bytes) and len(existing) == _KEY_BYTES:
            return existing

        generated = self._token_bytes(_KEY_BYTES)
        if not isinstance(generated, bytes) or len(generated) != _KEY_BYTES:
            raise ValueError("controller identity key generator must return 32 bytes")
        _atomic_write_bytes(self.key_path, generated)
        return generated


def compare_bindings(
    *,
    stored_digest: object,
    stored_scope_id: object,
    stored_version: object,
    live_digest: object,
    live_scope_id: object,
    live_version: object,
) -> DigestComparison:
    """Compare evidence without treating missing or re-keyed data as unequal."""

    if (
        not isinstance(stored_digest, str)
        or not isinstance(live_digest, str)
        or not isinstance(stored_scope_id, str)
        or not isinstance(live_scope_id, str)
        or stored_version != STABLE_IDENTIFIER_DIGEST_VERSION
        or live_version != STABLE_IDENTIFIER_DIGEST_VERSION
        or not hmac.compare_digest(stored_scope_id, live_scope_id)
    ):
        return DigestComparison.NOT_COMPARABLE
    if hmac.compare_digest(stored_digest, live_digest):
        return DigestComparison.SAME
    return DigestComparison.DIFFERENT


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish key bytes with the public stores' flush/fsync/replace contract."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                logger.debug("fsync unavailable for %s", temp_path, exc_info=True)
        os.replace(temp_path, path)
    except Exception:
        if fd != -1:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = [
    "DigestComparison",
    "KEY_FILENAME",
    "LastAppliedIdentity",
    "STABLE_IDENTIFIER_DIGEST_VERSION",
    "compare_bindings",
]
