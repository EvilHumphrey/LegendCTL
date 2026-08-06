from __future__ import annotations

import hashlib
import hmac
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zd_app.storage.last_applied_identity import (
    STABLE_IDENTIFIER_DIGEST_VERSION,
    DigestComparison,
    LastAppliedIdentity,
    compare_bindings,
)


_KEY_A = b"a" * 32
_KEY_B = b"b" * 32


class LastAppliedIdentityTests(unittest.TestCase):
    def test_key_is_generated_once_and_reused(self) -> None:
        with TemporaryDirectory() as tmp:
            identity = LastAppliedIdentity(tmp, token_bytes=lambda _size: _KEY_A)

            first = identity.binding("unit-a")
            second = identity.binding("unit-a")

            self.assertEqual(first, second)
            self.assertEqual(identity.key_path.read_bytes(), _KEY_A)

    def test_digest_is_full_casefolded_and_domain_separated(self) -> None:
        with TemporaryDirectory() as tmp:
            identity = LastAppliedIdentity(tmp, token_bytes=lambda _size: _KEY_A)
            digest, scope_id = identity.binding("  USB\\UNIT-A  ")

            self.assertEqual(
                (digest, scope_id),
                identity.binding("usb\\unit-a"),
            )
            self.assertEqual(len(digest), 64)
            self.assertEqual(len(scope_id), 12)
            container_domain_digest = hmac.new(
                _KEY_A,
                b"legendctl.instance.container.v1\0" + b"usb\\unit-a",
                hashlib.sha256,
            ).hexdigest()
            self.assertNotEqual(digest, container_domain_digest)

    def test_malformed_key_rotates_to_new_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            first = LastAppliedIdentity(tmp, token_bytes=lambda _size: _KEY_A)
            first_scope = first.binding("unit-a")[1]
            first.key_path.write_bytes(b"malformed")
            second = LastAppliedIdentity(tmp, token_bytes=lambda _size: _KEY_B)

            second_scope = second.binding("unit-a")[1]

            self.assertNotEqual(first_scope, second_scope)
            self.assertEqual(second.key_path.read_bytes(), _KEY_B)

    def test_atomic_write_failure_does_not_publish_key(self) -> None:
        with TemporaryDirectory() as tmp:
            identity = LastAppliedIdentity(tmp, token_bytes=lambda _size: _KEY_A)
            with patch(
                "zd_app.storage.last_applied_identity.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(OSError):
                    identity.binding("unit-a")

            self.assertFalse(identity.key_path.exists())
            self.assertEqual(list(identity.key_path.parent.glob(".*.tmp")), [])

    def test_comparison_preserves_not_comparable_semantics(self) -> None:
        with TemporaryDirectory() as first_tmp, TemporaryDirectory() as second_tmp:
            first_digest, first_scope = LastAppliedIdentity(
                first_tmp, token_bytes=lambda _size: _KEY_A
            ).binding("unit-a")
            second_digest, second_scope = LastAppliedIdentity(
                second_tmp, token_bytes=lambda _size: _KEY_B
            ).binding("unit-a")

        self.assertEqual(
            compare_bindings(
                stored_digest=first_digest,
                stored_scope_id=first_scope,
                stored_version=STABLE_IDENTIFIER_DIGEST_VERSION,
                live_digest=second_digest,
                live_scope_id=second_scope,
                live_version=STABLE_IDENTIFIER_DIGEST_VERSION,
            ),
            DigestComparison.NOT_COMPARABLE,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
