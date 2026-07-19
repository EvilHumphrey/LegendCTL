"""Provenance matrix — "What we know right now".

Display-only derivation over the device-state signals the app already holds.
Each EVIDENCE row gets exactly one of three honest provenance labels, and a
label may only ever DEGRADE downward on ambiguity
(``verified`` -> ``inferred`` -> ``unknown``) — never upward. No new device
I/O, no writes, no new fingerprint fields: this module reads primitive signals
gathered by the caller and maps them to labels + plain-language copy.

The ``applied`` row is NOT an evidence row: it derives from no signal at all,
so it carries the distinct non-evidence ``POLICY`` class and must never wear an
evidence chip. See ``POLICY`` and ``_applied_row``.

DPG-free by construction (mirrors ``trust_self_check`` / ``compatibility_report``)
so the label logic is unit-testable without a UI context.
"""

from __future__ import annotations

from dataclasses import dataclass

from zd_app.i18n import t


# Provenance labels, ordered by descending confidence. ``degrade downward``
# means a row can only move rightward in this tuple as its signal weakens.
VERIFIED = "verified"
INFERRED = "inferred"
UNKNOWN = "unknown"

PROVENANCE_ORDER = (VERIFIED, INFERRED, UNKNOWN)

# NOT a provenance level, and deliberately NOT in PROVENANCE_ORDER: this is the
# class for a row that reports a POLICY rather than an observation. It renders
# muted (see ``diagnostics._provenance_color``), never in the evidence color.
#
# A row that derives from no signal cannot be evidence, however honest its
# wording. An evidence chip in this grid means "we checked", and the other five
# rows all mean that literally — so a policy row wearing one reads as "we checked
# this apply" no matter what its copy says. Softening only the chip's WORDS while
# leaving the provenance (and therefore the green) intact is the exact failure
# mode this class exists to prevent.
POLICY = "policy"

# ``summary_sources`` values that populate the firmware / active-profile fields.
# Only ``SOURCE_PROTOCOL`` is a genuine device verification; ``SOURCE_OFFICIAL``
# is a UI Automation scrape of the official ZD app's window (not a device read).
SOURCE_PROTOCOL = "protocol"
SOURCE_OFFICIAL = "official_app_ui"

# Row-specific chip for a value read from the official ZD app's window. Colored
# like INFERRED (amber) because it is not device-verified; the ``label_key``
# override mechanism (see ``TrustMatrixRow``) supplies the distinct wording.
OFFICIAL_APP_LABEL_KEY = "trust_matrix.label.official_app"

# Row keys, in display order.
ROW_KEYS = ("identity", "firmware", "profile", "settings", "fingerprint", "applied")


@dataclass(frozen=True)
class TrustMatrixSignals:
    """The existing state signals each row derives from.

    Gathered by the UI layer from the shell/device-state so this module stays
    free of any shell/DPG coupling. Every field is a plain primitive.
    """

    connection_state: str
    data_freshness: str
    firmware_known: bool
    active_profile_known: bool
    # ``summary_sources`` values ("unknown" / "official_app_ui" / "protocol")
    # for the firmware and active-profile fields. These decide whether a KNOWN
    # value earns the "Verified from device" chip: only a genuine device read
    # (source "protocol") may. The sole current firmware source is an official
    # ZD app UI scrape ("official_app_ui") — NOT a device read — so it carries a
    # distinct "From the official app" chip instead of overclaiming verified.
    firmware_source: str
    profile_source: str
    # True only while a genuine protocol profile switch (0xd0 0x0f readback) is
    # still valid for the CURRENTLY-connected session. A ``profile_source`` of
    # "protocol" is STICKY across a disconnect (it is cleared only on an
    # identity change), so the source alone cannot distinguish a live-verified
    # slot from a retained one that may now be stale (the user can switch the
    # onboard profile on the controller while unplugged). The verified profile
    # chip therefore requires BOTH source=="protocol" AND this flag; a
    # same-unit reconnect leaves the flag False until a fresh protocol readback.
    active_profile_protocol_verified_this_connection: bool
    # A full settings read for the unit connected RIGHT NOW, this session.
    settings_read_current: bool
    # A retained settings snapshot exists at all (this or a prior connect).
    has_retained_settings: bool
    settings_skipped_fields: int
    fingerprint_present: bool
    fingerprint_complete: bool
    fingerprint_short_digest: str | None = None


@dataclass(frozen=True)
class TrustMatrixRow:
    key: str
    claim: str
    provenance: str
    why: str
    qualifier: str | None = None
    # Optional row-specific chip label key, for a row whose provenance CLASS is
    # right but whose default wording would overclaim. Sole user today: the
    # firmware row read from the official ZD app's window, which is colored like
    # INFERRED but must not say "Inferred locally" (see OFFICIAL_APP_LABEL_KEY).
    # ``provenance`` still drives color; only the displayed label is overridden.
    # Resolve through ``row_label``.
    #
    # The ``applied`` row does NOT use this. It is not an evidence row whose
    # wording needs softening — it is a different KIND of row, so it carries its
    # own provenance class (``POLICY``) and takes that class's label normally.
    # An override here would hide a wrong provenance behind nicer words.
    label_key: str | None = None


def build_trust_matrix(signals: TrustMatrixSignals) -> tuple[TrustMatrixRow, ...]:
    """Derive the six provenance rows from ``signals``.

    Pure and deterministic: same signals in, same rows out. Each branch degrades
    downward on ambiguity so retained/cached data can never earn ``verified``.
    """

    return (
        _identity_row(signals),
        _firmware_row(signals),
        _profile_row(signals),
        _settings_row(signals),
        _fingerprint_row(signals),
        _applied_row(),
    )


def _identity_row(signals: TrustMatrixSignals) -> TrustMatrixRow:
    # Connected -> the identity is read live right now. Disconnected but
    # previously connected (data_freshness stale is set ONLY on
    # connected->no_device) -> we are showing a retained identity. Otherwise
    # nothing has ever been recognized.
    if signals.connection_state == "connected":
        provenance = VERIFIED
    elif signals.data_freshness == "stale":
        provenance = INFERRED
    else:
        provenance = UNKNOWN
    return _keyed_row("identity", provenance)


def _firmware_row(signals: TrustMatrixSignals) -> TrustMatrixRow:
    # SOURCE-AWARE: the chip must reflect HOW the value was obtained, not merely
    # that it is known + connected. Only a genuine device read (source
    # "protocol") earns "Verified from device". The only firmware source that
    # exists today is the official ZD app UI scrape ("official_app_ui") — a read
    # of another app's window, NOT the controller — so it gets a distinct
    # "From the official app" chip (amber, INFERRED color) with honest why-copy.
    # A known value while disconnected is the retained "(last read)" class.
    connected = signals.connection_state == "connected"
    if signals.firmware_source == SOURCE_PROTOCOL and connected:
        # Future-proof: no protocol firmware read exists today. If one ever
        # lands, a live device read is genuinely verified.
        return _keyed_row("firmware", VERIFIED)
    if signals.firmware_source == SOURCE_OFFICIAL and connected:
        return _official_app_row("firmware")
    if signals.firmware_known:
        return _inferred_row("firmware", signals.firmware_source)
    return _keyed_row("firmware", UNKNOWN)


def _profile_row(signals: TrustMatrixSignals) -> TrustMatrixRow:
    # SOURCE-AWARE, same rule as firmware. The active profile has TWO populating
    # sources: a verified protocol switch (0xd0 0x0f readback — a real device
    # verification, keeps the "Verified from device" chip) and the official ZD
    # app UI scrape (the "From the official app" chip). Known-while-disconnected
    # degrades to the retained "(last read)" class.
    connected = signals.connection_state == "connected"
    if (
        signals.profile_source == SOURCE_PROTOCOL
        and connected
        and signals.active_profile_protocol_verified_this_connection
    ):
        # "Verified from device" ONLY while the protocol readback is still valid
        # for the currently-connected session. A ``protocol`` source is sticky
        # across a disconnect (cleared only on identity change), so without the
        # flag a same-unit reconnect showing a stale slot would falsely re-green.
        return _keyed_row("profile", VERIFIED)
    if signals.profile_source == SOURCE_OFFICIAL and connected:
        return _official_app_row("profile")
    if signals.active_profile_known:
        return _inferred_row("profile", signals.profile_source)
    return _keyed_row("profile", UNKNOWN)


def _official_app_row(key: str) -> TrustMatrixRow:
    # A value scraped from the official ZD app's window. Structurally it can
    # NEVER read "Verified from device": provenance is INFERRED (drives the
    # amber color) and the chip label is overridden to "From the official app".
    return TrustMatrixRow(
        key=key,
        claim=t(f"trust_matrix.row.{key}.claim"),
        provenance=INFERRED,
        why=t(f"trust_matrix.row.{key}.why.official_app"),
        label_key=OFFICIAL_APP_LABEL_KEY,
    )


def _inferred_row(key: str, source: str) -> TrustMatrixRow:
    # SOURCE-AWARE retained ("last read") copy. A value retained from a genuine
    # device read gets the plain ``why.inferred`` ("earlier read"). A value
    # whose last source was the official ZD app UI scrape ("official_app_ui")
    # was never read from the controller at all, so it MUST NOT imply a device
    # read — it gets the honest ``why.inferred_official_app`` variant instead.
    if source == SOURCE_OFFICIAL:
        why_key = f"trust_matrix.row.{key}.why.inferred_official_app"
    else:
        why_key = f"trust_matrix.row.{key}.why.inferred"
    return TrustMatrixRow(
        key=key,
        claim=t(f"trust_matrix.row.{key}.claim"),
        provenance=INFERRED,
        why=t(why_key),
    )


def _settings_row(signals: TrustMatrixSignals) -> TrustMatrixRow:
    qualifier: str | None = None
    if signals.settings_read_current:
        provenance = VERIFIED
        if signals.settings_skipped_fields > 0:
            # Reuse the apply.read.partial_budget honesty: a budget-truncated
            # read is still verified for what it DID read, with an explicit
            # "N fields not read" line.
            qualifier = t(
                "trust_matrix.row.settings.qualifier.partial",
                count=signals.settings_skipped_fields,
            )
    elif signals.has_retained_settings:
        # A snapshot survives from a previous read but does not belong to the
        # unit connected now (or nothing is connected) -> retained, not current.
        provenance = INFERRED
    else:
        provenance = UNKNOWN
    return _keyed_row("settings", provenance, qualifier)


def _fingerprint_row(signals: TrustMatrixSignals) -> TrustMatrixRow:
    # The fingerprint is collected live on connect and cleared on disconnect
    # (device_service), so it is verified-or-nothing — there is no retained
    # fingerprint to label "inferred". A present-but-incomplete (inventory-only)
    # fingerprint is verified for the fields it DID observe, with a
    # "some fields not collected" qualifier.
    digest = signals.fingerprint_short_digest or t("model_fingerprint.value.not_collected")
    qualifier: str | None = None
    if not signals.fingerprint_present:
        provenance = UNKNOWN
    elif signals.fingerprint_complete:
        provenance = VERIFIED
    else:
        provenance = VERIFIED
        qualifier = t("trust_matrix.row.fingerprint.qualifier.partial")
    return TrustMatrixRow(
        key="fingerprint",
        claim=t("trust_matrix.row.fingerprint.claim"),
        provenance=provenance,
        why=t(f"trust_matrix.row.fingerprint.why.{provenance}", digest=digest),
        qualifier=qualifier,
    )


def _applied_row() -> TrustMatrixRow:
    # Signal-free BY CONSTRUCTION — this row intentionally takes no arguments.
    # Phase 1 gives profile Apply a per-run post-write read-back sweep, but this
    # Phase 2 copy change does not give the matrix that signal. It remains a
    # verification-SCOPE statement, so it must wear the non-evidence POLICY chip
    # and render muted.
    #
    # Restore, Safe Import, settled inline-deadzone changes, and profile Apply
    # now read every readable field back after writing. Profile Apply exposes
    # its per-field outcomes in apply details; write-only settings are reported
    # honestly as sent because they cannot be read back.
    #
    # DO NOT make this evidence or accept ApplyResult here until Phase 3 derives
    # a real per-run matrix signal from the sweep.
    return TrustMatrixRow(
        key="applied",
        claim=t("trust_matrix.row.applied.claim"),
        provenance=POLICY,
        why=t("trust_matrix.row.applied.why"),
    )


def _keyed_row(key: str, provenance: str, qualifier: str | None = None) -> TrustMatrixRow:
    return TrustMatrixRow(
        key=key,
        claim=t(f"trust_matrix.row.{key}.claim"),
        provenance=provenance,
        why=t(f"trust_matrix.row.{key}.why.{provenance}"),
        qualifier=qualifier,
    )


def provenance_label(provenance: str) -> str:
    """User-facing label for a provenance constant."""

    return t(f"trust_matrix.label.{provenance}")


def row_label(row: TrustMatrixRow) -> str:
    """User-facing chip label for a row (honoring its row-specific override)."""

    if row.label_key is not None:
        return t(row.label_key)
    return provenance_label(row.provenance)


__all__ = [
    "INFERRED",
    "OFFICIAL_APP_LABEL_KEY",
    "POLICY",
    "PROVENANCE_ORDER",
    "ROW_KEYS",
    "SOURCE_OFFICIAL",
    "SOURCE_PROTOCOL",
    "UNKNOWN",
    "VERIFIED",
    "TrustMatrixRow",
    "TrustMatrixSignals",
    "build_trust_matrix",
    "provenance_label",
    "row_label",
]
