"""Dataclasses for Module Passport — per-side stick-module characterization.

A controller has two passports (left + right). Each passport identifies the
currently-installed module by an operator-assigned ``module_id`` and carries
an append-only history of :class:`ModuleFingerprint` records: one per
60-second characterization run. Swapping a side archives the whole previous
passport and starts a fresh one.

Design choices: per-side semantics throughout, no controller-level passport,
manual ``module_id`` (no auto-detection in v1), characterization is
point-in-time (no live ambient overlay).

The 8 metrics each fingerprint records:

- ``noise_floor_percent``         p99 radial deviation at rest (percent of full travel)
- ``centering_offset_x``          median x at rest
- ``centering_offset_y``          median y at rest
- ``circularity_coverage_percent`` share of 32 polar sectors at >=90% of max radius
- ``outer_deadzone_min_axis``     min absolute axis value at extreme travel
- ``outer_deadzone_max_axis``     max absolute axis value at extreme travel
- ``asymmetry_score``             quadrant-density variance (0 = symmetric)
- ``bitness_observed``            unique discrete-step count during settle
- ``tremor_metric``               high-frequency jitter proxy at rest
- ``linearity_score``             deviation from ideal linear response during rotation

Plus ``overall_status`` reducing the 8 metrics to a 3-band verdict
(``good`` / ``watch`` / ``wear_observed``).

JSON codec lives at module scope: :func:`module_passport_to_dict` /
:func:`module_passport_from_dict` round-trip the whole record so the
service layer can write one file per side without touching dataclass
internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


SIDE_LEFT = "left"
SIDE_RIGHT = "right"
SIDES: tuple[str, ...] = (SIDE_LEFT, SIDE_RIGHT)


STATUS_GOOD = "good"
STATUS_WATCH = "watch"
STATUS_WEAR_OBSERVED = "wear_observed"
OVERALL_STATUSES: tuple[str, ...] = (STATUS_GOOD, STATUS_WATCH, STATUS_WEAR_OBSERVED)


MAX_MODULE_ID_LEN = 200
MAX_MODULE_NOTES_LEN = 2000


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")


def sanitize_module_id(text: str) -> str:
    """Single-line freeform identifier, capped at :data:`MAX_MODULE_ID_LEN`.

    Strips C0 controls, collapses any internal whitespace run (including
    newlines and tabs) to a single space, trims, and caps. Returns an empty
    string when the input is empty or sanitises to nothing — the service
    layer treats empty as "no assignment yet" and the UI surface gates
    the assign button on a non-empty result.
    """

    if not text:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    cleaned = _WHITESPACE_COLLAPSE_RE.sub(" ", cleaned).strip()
    if len(cleaned) > MAX_MODULE_ID_LEN:
        cleaned = cleaned[:MAX_MODULE_ID_LEN].rstrip()
    return cleaned


def sanitize_module_notes(text: str) -> str:
    """Multi-line freeform note, capped at :data:`MAX_MODULE_NOTES_LEN`.

    Mirrors :func:`zd_app.services.wear_ledger.models.sanitize_service_note`
    but lives here so module_passport_models doesn't import the wear-ledger
    package. CRLF/CR normalise to LF, C0 controls strip, trailing whitespace
    per line trims, runs of >=3 newlines collapse to 2, then global trim,
    then cap.
    """

    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > MAX_MODULE_NOTES_LEN:
        cleaned = cleaned[:MAX_MODULE_NOTES_LEN].rstrip()
    return cleaned


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleFingerprint:
    """One characterization run's metric snapshot.

    All numeric fields are populated even when sample count is too low to
    score the band — :attr:`overall_status` reflects insufficient-sample
    runs as ``wear_observed`` (conservative: a thin sample shouldn't pass
    as ``good``).

    See module docstring for per-metric semantics.
    """

    timestamp_utc: str
    side: str
    duration_ms: int
    samples_count: int
    noise_floor_percent: float
    centering_offset_x: float
    centering_offset_y: float
    circularity_coverage_percent: float
    outer_deadzone_min_axis: float
    outer_deadzone_max_axis: float
    asymmetry_score: float
    bitness_observed: int
    tremor_metric: float
    linearity_score: float
    overall_status: str
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "side": self.side,
            "duration_ms": int(self.duration_ms),
            "samples_count": int(self.samples_count),
            "noise_floor_percent": float(self.noise_floor_percent),
            "centering_offset_x": float(self.centering_offset_x),
            "centering_offset_y": float(self.centering_offset_y),
            "circularity_coverage_percent": float(self.circularity_coverage_percent),
            "outer_deadzone_min_axis": float(self.outer_deadzone_min_axis),
            "outer_deadzone_max_axis": float(self.outer_deadzone_max_axis),
            "asymmetry_score": float(self.asymmetry_score),
            "bitness_observed": int(self.bitness_observed),
            "tremor_metric": float(self.tremor_metric),
            "linearity_score": float(self.linearity_score),
            "overall_status": self.overall_status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModuleFingerprint":
        timestamp = payload.get("timestamp_utc")
        side = payload.get("side")
        if not isinstance(timestamp, str) or not isinstance(side, str):
            raise ValueError("ModuleFingerprint requires str timestamp_utc and side")
        if side not in SIDES:
            # ``side`` flows into archive filenames; reject anything but the two
            # canonical sides so a tampered passport can't smuggle a path here.
            raise ValueError(f"ModuleFingerprint side must be one of {SIDES}; got {side!r}")
        overall = payload.get("overall_status")
        if not isinstance(overall, str):
            raise ValueError("ModuleFingerprint requires str overall_status")
        notes = payload.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("ModuleFingerprint.notes must be str or None")
        return cls(
            timestamp_utc=timestamp,
            side=side,
            duration_ms=int(payload.get("duration_ms", 0)),
            samples_count=int(payload.get("samples_count", 0)),
            noise_floor_percent=float(payload.get("noise_floor_percent", 0.0)),
            centering_offset_x=float(payload.get("centering_offset_x", 0.0)),
            centering_offset_y=float(payload.get("centering_offset_y", 0.0)),
            circularity_coverage_percent=float(
                payload.get("circularity_coverage_percent", 0.0)
            ),
            outer_deadzone_min_axis=float(payload.get("outer_deadzone_min_axis", 0.0)),
            outer_deadzone_max_axis=float(payload.get("outer_deadzone_max_axis", 0.0)),
            asymmetry_score=float(payload.get("asymmetry_score", 0.0)),
            bitness_observed=int(payload.get("bitness_observed", 0)),
            tremor_metric=float(payload.get("tremor_metric", 0.0)),
            linearity_score=float(payload.get("linearity_score", 0.0)),
            overall_status=overall,
            notes=notes,
        )


@dataclass(frozen=True)
class ModulePassport:
    """Per-side stick-module identity + characterization history.

    Fingerprints are append-only; the most recent run is conventionally the
    last entry in :attr:`fingerprints`. Reassigning a module to this side is
    a separate operation that the service layer handles by archiving the
    whole prior passport and writing a fresh one.
    """

    side: str
    module_id: str
    assigned_at_utc: str
    notes: str
    fingerprints: tuple[ModuleFingerprint, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "side": self.side,
            "module_id": self.module_id,
            "assigned_at_utc": self.assigned_at_utc,
            "notes": self.notes,
            "fingerprints": [fp.to_dict() for fp in self.fingerprints],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModulePassport":
        side = payload.get("side")
        module_id = payload.get("module_id")
        assigned_at = payload.get("assigned_at_utc")
        if not isinstance(side, str):
            raise ValueError("ModulePassport requires str side")
        if side not in SIDES:
            # ``side`` is used to build the on-disk archive path; only the two
            # canonical sides are valid, so a tampered ``side`` (e.g. a traversal
            # string) is rejected at the load boundary.
            raise ValueError(f"ModulePassport side must be one of {SIDES}; got {side!r}")
        if not isinstance(module_id, str):
            raise ValueError("ModulePassport requires str module_id")
        if not isinstance(assigned_at, str):
            raise ValueError("ModulePassport requires str assigned_at_utc")
        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError("ModulePassport.notes must be a string")
        schema_version = int(payload.get("schema_version", SCHEMA_VERSION))
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported module_passport schema_version: {schema_version}"
            )
        raw_fingerprints = payload.get("fingerprints", []) or []
        if not isinstance(raw_fingerprints, Iterable):
            raise ValueError("ModulePassport.fingerprints must be iterable")
        fingerprints: list[ModuleFingerprint] = []
        for raw in raw_fingerprints:
            if not isinstance(raw, Mapping):
                raise ValueError("ModulePassport.fingerprints entries must be mappings")
            fingerprints.append(ModuleFingerprint.from_dict(raw))
        return cls(
            side=side,
            module_id=module_id,
            assigned_at_utc=assigned_at,
            notes=notes,
            fingerprints=tuple(fingerprints),
            schema_version=schema_version,
        )

    def with_fingerprint(self, fingerprint: ModuleFingerprint) -> "ModulePassport":
        """Return a new passport with one more fingerprint appended."""

        return replace(self, fingerprints=self.fingerprints + (fingerprint,))

    def with_notes(self, notes: str) -> "ModulePassport":
        """Return a new passport with the notes field replaced."""

        return replace(self, notes=notes)

    def latest_fingerprint(self) -> Optional[ModuleFingerprint]:
        """Most-recently-appended fingerprint, or ``None`` if none recorded."""

        if not self.fingerprints:
            return None
        return self.fingerprints[-1]


# ---------------------------------------------------------------------------
# Module-level codec aliases — for parity with restore_point_models.
# ---------------------------------------------------------------------------


def module_passport_to_dict(passport: ModulePassport) -> dict[str, Any]:
    return passport.to_dict()


def module_passport_from_dict(payload: Mapping[str, Any]) -> ModulePassport:
    return ModulePassport.from_dict(payload)


__all__ = [
    "MAX_MODULE_ID_LEN",
    "MAX_MODULE_NOTES_LEN",
    "ModuleFingerprint",
    "ModulePassport",
    "OVERALL_STATUSES",
    "SCHEMA_VERSION",
    "SIDE_LEFT",
    "SIDE_RIGHT",
    "SIDES",
    "STATUS_GOOD",
    "STATUS_WATCH",
    "STATUS_WEAR_OBSERVED",
    "SUPPORTED_SCHEMA_VERSIONS",
    "module_passport_from_dict",
    "module_passport_to_dict",
    "sanitize_module_id",
    "sanitize_module_notes",
]
