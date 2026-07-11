"""Per-row label derivation for the provenance ("What we know right now") matrix.

DPG-free: exercises the pure ``build_trust_matrix`` service so every branch of
the verified/inferred/unknown logic is pinned, including the never-upgrade
guard (retained/cached data must never earn ``verified``).
"""

from __future__ import annotations

import unittest

from zd_app import i18n
from zd_app.services import trust_matrix
from zd_app.services.trust_matrix import (
    INFERRED,
    OFFICIAL_APP_LABEL_KEY,
    UNKNOWN,
    VERIFIED,
    TrustMatrixSignals,
    build_trust_matrix,
)


# Row index -> key, mirrors services.trust_matrix.ROW_KEYS display order.
IDENTITY, FIRMWARE, PROFILE, SETTINGS, FINGERPRINT, APPLIED = range(6)


def _signals(**overrides) -> TrustMatrixSignals:
    base = dict(
        connection_state="no_device",
        data_freshness="never_read",
        firmware_known=False,
        active_profile_known=False,
        firmware_source="unknown",
        profile_source="unknown",
        active_profile_protocol_verified_this_connection=False,
        settings_read_current=False,
        has_retained_settings=False,
        settings_skipped_fields=0,
        fingerprint_present=False,
        fingerprint_complete=False,
        fingerprint_short_digest=None,
    )
    base.update(overrides)
    return TrustMatrixSignals(**base)


def _connected_fresh(**overrides) -> TrustMatrixSignals:
    base = dict(
        connection_state="connected",
        data_freshness="fresh",
        firmware_known=True,
        active_profile_known=True,
        # Sources default to "protocol" so the baseline is the all-verified
        # shape. Firmware "protocol" is future-proof (no such source exists in
        # production today); tests that exercise the real official-app source
        # override these explicitly.
        firmware_source="protocol",
        profile_source="protocol",
        # Baseline is a genuine live protocol verification for THIS connection,
        # so the profile row earns "Verified from device". Reconnect-stale tests
        # override this to False to prove the row degrades to inferred.
        active_profile_protocol_verified_this_connection=True,
        settings_read_current=True,
        has_retained_settings=True,
        settings_skipped_fields=0,
        fingerprint_present=True,
        fingerprint_complete=True,
        fingerprint_short_digest="abc123def456",
    )
    base.update(overrides)
    return TrustMatrixSignals(**base)


class TrustMatrixDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n._loaded.clear()
        i18n._reverse_en.clear()
        i18n.set_locale("en")

    def _provenances(self, signals) -> list[str]:
        return [row.provenance for row in build_trust_matrix(signals)]

    def test_row_order_and_keys_are_stable(self) -> None:
        rows = build_trust_matrix(_signals())
        self.assertEqual(
            tuple(row.key for row in rows),
            trust_matrix.ROW_KEYS,
        )

    def test_all_verified_when_connected_fresh_full_read(self) -> None:
        provs = self._provenances(_connected_fresh())
        self.assertEqual(
            provs,
            [VERIFIED, VERIFIED, VERIFIED, VERIFIED, VERIFIED, VERIFIED],
        )

    def test_disconnected_retained_degrades_to_inferred(self) -> None:
        # Was connected (data_freshness stale), values retained, no live unit.
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            firmware_known=True,
            active_profile_known=True,
            settings_read_current=False,
            has_retained_settings=True,
            fingerprint_present=False,
        )
        provs = self._provenances(signals)
        self.assertEqual(provs[IDENTITY], INFERRED)
        self.assertEqual(provs[FIRMWARE], INFERRED)
        self.assertEqual(provs[PROFILE], INFERRED)
        self.assertEqual(provs[SETTINGS], INFERRED)
        # Fingerprint is verified-or-nothing (cleared on disconnect) -> unknown.
        self.assertEqual(provs[FINGERPRINT], UNKNOWN)
        # Applied-verification row is a static mechanism statement.
        self.assertEqual(provs[APPLIED], VERIFIED)

    def test_never_connected_is_all_unknown(self) -> None:
        provs = self._provenances(_signals())
        self.assertEqual(provs[IDENTITY], UNKNOWN)
        self.assertEqual(provs[FIRMWARE], UNKNOWN)
        self.assertEqual(provs[PROFILE], UNKNOWN)
        self.assertEqual(provs[SETTINGS], UNKNOWN)
        self.assertEqual(provs[FINGERPRINT], UNKNOWN)
        self.assertEqual(provs[APPLIED], VERIFIED)

    def test_never_upgrade_guard_retained_settings_are_never_verified(self) -> None:
        # A retained snapshot exists but does NOT belong to the unit connected
        # now (settings_read_current False) -> must be inferred, never verified.
        signals = _connected_fresh(settings_read_current=False)
        rows = build_trust_matrix(signals)
        self.assertEqual(rows[SETTINGS].provenance, INFERRED)
        self.assertNotEqual(rows[SETTINGS].provenance, VERIFIED)

    def test_never_upgrade_guard_disconnected_firmware_is_never_verified(self) -> None:
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            firmware_known=True,
        )
        rows = build_trust_matrix(signals)
        self.assertEqual(rows[FIRMWARE].provenance, INFERRED)

    def test_partial_read_is_verified_with_count_qualifier(self) -> None:
        signals = _connected_fresh(settings_skipped_fields=3)
        row = build_trust_matrix(signals)[SETTINGS]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNotNone(row.qualifier)
        self.assertIn("3", row.qualifier)

    def test_full_read_has_no_settings_qualifier(self) -> None:
        row = build_trust_matrix(_connected_fresh(settings_skipped_fields=0))[SETTINGS]
        self.assertIsNone(row.qualifier)

    def test_incomplete_fingerprint_is_verified_with_qualifier(self) -> None:
        signals = _connected_fresh(
            fingerprint_present=True,
            fingerprint_complete=False,
            fingerprint_short_digest="deadbeef0000",
        )
        row = build_trust_matrix(signals)[FINGERPRINT]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNotNone(row.qualifier)

    def test_complete_fingerprint_why_carries_digest(self) -> None:
        signals = _connected_fresh(fingerprint_short_digest="abc123def456")
        row = build_trust_matrix(signals)[FINGERPRINT]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNone(row.qualifier)
        self.assertIn("abc123def456", row.why)

    def test_applied_row_disclaims_ack_only_verification(self) -> None:
        row = build_trust_matrix(_signals())[APPLIED]
        # Doctrine: a write ACK alone is never presented as verified. The static
        # copy must say so explicitly, and must state the DISPLAY RULE ("only
        # marked verified after ... read back") rather than the universal
        # "every write is read back" mechanism overclaim (overseer honesty fix,
        # 2026-07-05 — same class as the killed step-size docs wording).
        self.assertIn("acknowledgement", row.why.lower())
        self.assertIn("only marked verified", row.why.lower())
        self.assertNotIn("are confirmed by reading", row.why.lower())

    def test_applied_row_chip_is_read_back_not_verified_from_device(self) -> None:
        # The applied row is a static policy row — no device was consulted at
        # render time, so its chip must NOT read "Verified from device"; it
        # carries the row-specific "Verified by read-back" label instead.
        row = build_trust_matrix(_signals())[APPLIED]
        self.assertEqual(row.label_key, "trust_matrix.label.applied")
        label = trust_matrix.row_label(row)
        self.assertEqual(label, i18n.t("trust_matrix.label.applied"))
        self.assertNotEqual(label, trust_matrix.provenance_label(VERIFIED))
        self.assertFalse(label.startswith("["), "missing trust_matrix.label.applied key")
        # Every other row keeps the shared provenance chip.
        for other in build_trust_matrix(_connected_fresh())[:APPLIED]:
            self.assertIsNone(other.label_key)
            self.assertEqual(
                trust_matrix.row_label(other),
                trust_matrix.provenance_label(other.provenance),
            )

    # --- Source-aware firmware/profile chips -------------------------------

    def test_firmware_official_app_source_gets_official_chip_not_verified(self) -> None:
        # The only firmware source today is the official ZD app UI scrape. It is
        # NOT a device read, so connected + known must show the "From the
        # official app" chip (amber/INFERRED color), never "Verified from device".
        signals = _connected_fresh(firmware_source="official_app_ui")
        row = build_trust_matrix(signals)[FIRMWARE]
        self.assertEqual(row.label_key, OFFICIAL_APP_LABEL_KEY)
        self.assertEqual(row.provenance, INFERRED)
        label = trust_matrix.row_label(row)
        self.assertEqual(label, i18n.t("trust_matrix.label.official_app"))
        self.assertFalse(label.startswith("["), "missing official_app label key")
        self.assertNotEqual(label, trust_matrix.provenance_label(VERIFIED))
        self.assertIn("official ZD app", row.why)

    def test_profile_official_app_source_gets_official_chip_not_verified(self) -> None:
        signals = _connected_fresh(profile_source="official_app_ui")
        row = build_trust_matrix(signals)[PROFILE]
        self.assertEqual(row.label_key, OFFICIAL_APP_LABEL_KEY)
        self.assertEqual(row.provenance, INFERRED)
        label = trust_matrix.row_label(row)
        self.assertEqual(label, i18n.t("trust_matrix.label.official_app"))
        self.assertNotEqual(label, trust_matrix.provenance_label(VERIFIED))
        self.assertIn("official ZD app", row.why)

    def test_profile_protocol_source_is_verified_switch_ack_copy(self) -> None:
        # A verified protocol switch (0xd0 0x0f readback) IS a genuine device
        # verification, so it keeps the "Verified from device" chip; the why-copy
        # names the controller's own switch acknowledgement, not a generic read.
        signals = _connected_fresh(profile_source="protocol")
        row = build_trust_matrix(signals)[PROFILE]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNone(row.label_key)
        self.assertEqual(
            trust_matrix.row_label(row), trust_matrix.provenance_label(VERIFIED)
        )
        self.assertIn("switch acknowledgement", row.why.lower())

    def test_firmware_protocol_source_is_verified_future_proof(self) -> None:
        # No protocol firmware read exists today, but if one ever lands, a live
        # device read is genuinely verified.
        signals = _connected_fresh(firmware_source="protocol")
        row = build_trust_matrix(signals)[FIRMWARE]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNone(row.label_key)

    def test_official_app_source_while_disconnected_is_retained_inferred(self) -> None:
        # Official-app value but the controller is gone -> the retained
        # "(last read)" class, NOT the live "From the official app" chip.
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            firmware_known=True,
            active_profile_known=True,
            firmware_source="official_app_ui",
            profile_source="official_app_ui",
        )
        rows = build_trust_matrix(signals)
        self.assertEqual(rows[FIRMWARE].provenance, INFERRED)
        self.assertIsNone(rows[FIRMWARE].label_key)
        self.assertEqual(rows[PROFILE].provenance, INFERRED)
        self.assertIsNone(rows[PROFILE].label_key)

    def test_firmware_unknown_copy_names_missing_field_not_generic(self) -> None:
        # Reworded honest copy: a normal LegendCTL read has no firmware field.
        row = build_trust_matrix(_signals())[FIRMWARE]
        self.assertEqual(row.provenance, UNKNOWN)
        self.assertIn("do not include a firmware field", row.why)

    def test_profile_unknown_copy_names_missing_slot_not_generic(self) -> None:
        row = build_trust_matrix(_signals())[PROFILE]
        self.assertEqual(row.provenance, UNKNOWN)
        self.assertIn("does not report the active slot", row.why)

    def test_honesty_guard_official_app_source_never_labels_verified(self) -> None:
        # PINNED HONESTY GUARD (structural, like the never-upgrade guards): across
        # EVERY combination of connection/freshness/known flags, an
        # "official_app_ui" firmware or profile source may never produce the
        # "Verified from device" chip. Chipping a UIA scrape of another app's
        # window as a device read is exactly the overclaim this project kills.
        verified_label = trust_matrix.provenance_label(VERIFIED)
        connection_states = ("connected", "no_device")
        freshness_states = ("fresh", "stale", "never_read")
        for conn in connection_states:
            for fresh in freshness_states:
                for known in (True, False):
                    signals = _signals(
                        connection_state=conn,
                        data_freshness=fresh,
                        firmware_known=known,
                        active_profile_known=known,
                        firmware_source="official_app_ui",
                        profile_source="official_app_ui",
                    )
                    rows = build_trust_matrix(signals)
                    for index in (FIRMWARE, PROFILE):
                        row = rows[index]
                        with self.subTest(conn=conn, fresh=fresh, known=known, row=index):
                            self.assertNotEqual(row.provenance, VERIFIED)
                            self.assertNotEqual(
                                trust_matrix.row_label(row), verified_label
                            )

    # --- Defect 1: protocol source is sticky; the flag gates verified --------

    def test_profile_protocol_verified_this_connection_is_verified(self) -> None:
        # No false-negative: a genuine live protocol verification for THIS
        # connection DOES earn "Verified from device".
        signals = _connected_fresh(
            profile_source="protocol",
            active_profile_protocol_verified_this_connection=True,
        )
        row = build_trust_matrix(signals)[PROFILE]
        self.assertEqual(row.provenance, VERIFIED)
        self.assertIsNone(row.label_key)

    def test_profile_protocol_source_reconnect_stale_is_inferred_not_verified(self) -> None:
        # DEFECT 1 REPRO (at the matrix boundary): the protocol source is sticky
        # across a disconnect, so a same-unit reconnect arrives with
        # profile_source=="protocol" and connection_state=="connected" BUT the
        # protocol readback is no longer valid for this connection (flag False,
        # cleared on the disconnect). The row MUST render inferred/retained, not
        # re-green to "Verified from device" while showing a possibly-stale slot.
        signals = _connected_fresh(
            profile_source="protocol",
            active_profile_protocol_verified_this_connection=False,
            active_profile_known=True,
        )
        row = build_trust_matrix(signals)[PROFILE]
        self.assertEqual(row.provenance, INFERRED)
        self.assertNotEqual(row.provenance, VERIFIED)
        self.assertIsNone(row.label_key)
        self.assertNotEqual(
            trust_matrix.row_label(row), trust_matrix.provenance_label(VERIFIED)
        )
        # Retained value class copy (a genuine device read went stale), NOT the
        # official-app variant — the last source was a real protocol readback.
        self.assertEqual(row.why, i18n.t("trust_matrix.row.profile.why.inferred"))

    def test_never_upgrade_guard_protocol_without_flag_never_verified(self) -> None:
        # PINNED STRUCTURAL GUARD: across EVERY connection/freshness/known combo,
        # a "protocol" profile source WITHOUT a live this-connection readback flag
        # may never produce the "Verified from device" chip. This is the exact
        # sticky-source re-green the disconnect downgrade kills.
        verified_label = trust_matrix.provenance_label(VERIFIED)
        for conn in ("connected", "no_device"):
            for fresh in ("fresh", "stale", "never_read"):
                for known in (True, False):
                    signals = _signals(
                        connection_state=conn,
                        data_freshness=fresh,
                        active_profile_known=known,
                        profile_source="protocol",
                        active_profile_protocol_verified_this_connection=False,
                    )
                    row = build_trust_matrix(signals)[PROFILE]
                    with self.subTest(conn=conn, fresh=fresh, known=known):
                        self.assertNotEqual(row.provenance, VERIFIED)
                        self.assertNotEqual(
                            trust_matrix.row_label(row), verified_label
                        )

    # --- Defect 2: inferred copy must be source-aware ------------------------

    def test_inferred_firmware_official_app_source_uses_official_copy(self) -> None:
        # DEFECT 2: an official-app-sourced firmware that goes stale on
        # disconnect must NOT claim it was "read from the controller". The
        # inferred copy is the *_official_app variant.
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            firmware_known=True,
            firmware_source="official_app_ui",
        )
        row = build_trust_matrix(signals)[FIRMWARE]
        self.assertEqual(row.provenance, INFERRED)
        self.assertEqual(
            row.why, i18n.t("trust_matrix.row.firmware.why.inferred_official_app")
        )
        self.assertNotEqual(
            row.why, i18n.t("trust_matrix.row.firmware.why.inferred")
        )
        self.assertIn("official ZD app", row.why)

    def test_inferred_profile_official_app_source_uses_official_copy(self) -> None:
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            active_profile_known=True,
            profile_source="official_app_ui",
        )
        row = build_trust_matrix(signals)[PROFILE]
        self.assertEqual(row.provenance, INFERRED)
        self.assertEqual(
            row.why, i18n.t("trust_matrix.row.profile.why.inferred_official_app")
        )
        self.assertNotEqual(
            row.why, i18n.t("trust_matrix.row.profile.why.inferred")
        )
        self.assertIn("official ZD app", row.why)

    def test_inferred_device_retained_keeps_plain_earlier_read_copy(self) -> None:
        # A genuinely device-retained firmware/profile (source not official_app)
        # keeps the plain "earlier read" copy — the *_official_app variant would
        # be wrong here.
        signals = _signals(
            connection_state="no_device",
            data_freshness="stale",
            firmware_known=True,
            active_profile_known=True,
            firmware_source="protocol",
            profile_source="protocol",
        )
        rows = build_trust_matrix(signals)
        self.assertEqual(
            rows[FIRMWARE].why, i18n.t("trust_matrix.row.firmware.why.inferred")
        )
        self.assertEqual(
            rows[PROFILE].why, i18n.t("trust_matrix.row.profile.why.inferred")
        )

    def test_labels_resolve_for_every_provenance(self) -> None:
        for provenance in (VERIFIED, INFERRED, UNKNOWN):
            label = trust_matrix.provenance_label(provenance)
            self.assertTrue(label)
            self.assertFalse(label.startswith("["), f"missing label key for {provenance}")


if __name__ == "__main__":
    unittest.main()
