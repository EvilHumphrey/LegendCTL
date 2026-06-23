"""DiagnosticBundleService — aggregate wrapper records into a shareable bundle.

Two entry points:

- :meth:`generate_markdown` returns the rendered Markdown report as a
  string (pure — no file I/O). Used by the UI's "Markdown only" path and
  by every unit test that asserts content / sanitization.
- :meth:`generate_bundle_zip` writes a ZIP file to ``target_path``
  containing the Markdown report plus raw JSON copies of the underlying
  records: module passports (active + archived), the N most-recent Health
  Reports, and a summarized wear-ledger roll-up. The raw wear-ledger
  ``events.jsonl`` is NOT included — only a privacy-conservative summary.

Both calls emit a single wear-ledger event
(:data:`zd_app.services.wear_ledger.models.DIAGNOSTIC_BUNDLE_GENERATED`)
on success.

The service is best-effort at every data-source boundary: missing module
passport → "Not assigned" placeholder; missing health_reports directory →
"No exports found" section line; empty wear ledger → all zeroes. Bundle
generation never raises through these holes.

**Path sanitization.** The on-disk Markdown must not leak absolute paths,
even when an embedded summary line happens to include one (e.g., a wear
ledger ``details`` dict that recorded a profile path). All paths
flowing into the report run through :func:`_sanitize_path`, which
rewrites ``<APP_DATA>\\ZDUltimateLegend\\...`` segments with the
:data:`APP_DATA_PLACEHOLDER` token and strips the dirname when the file
is inside one of the wrapper's own known sub-directories.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from zd_app.services.diagnostic_bundle.boundary import CLAIM_BOUNDARY_PARAGRAPH
from zd_app.services.path_scrub import APP_DATA_PLACEHOLDER, scrub_value
from zd_app.services.module_passport import ModulePassportService
from zd_app.services.wear_ledger import WearLedgerService
from zd_app.services.wear_ledger.models import (
    DIAGNOSTIC_BUNDLE_GENERATED,
    HEALTH_REPORT,
    MODULE_ASSIGNED,
    MODULE_CHARACTERIZED,
    MODULE_NOTES_UPDATED,
    PROFILE_APPLY,
    READINESS_CHECK,
    RP_CAPTURE,
    RP_RESTORE,
    SERVICE_NOTE,
    SESSION_START,
)
from zd_app.storage.module_passport_models import (
    SIDE_LEFT,
    SIDE_RIGHT,
    SIDES,
    ModuleFingerprint,
    ModulePassport,
)
from zd_app.version import __build_commit__, __version__


logger = logging.getLogger(__name__)


BUNDLE_DIRNAME = "diagnostic_bundles"
# APP_DATA_PLACEHOLDER is imported from path_scrub and re-exported here for
# backward compatibility (tests + the package __init__ import it from this
# module).


# Default summary windows / limits.
DEFAULT_HEALTH_REPORT_LIMIT = 5
DEFAULT_WEAR_LEDGER_DAYS = 90
DEFAULT_RECENT_SERVICE_NOTES = 10


# Format constants the Markdown renderer leans on. Centralised so tests
# that scan for them have a stable anchor.
HEADING_TOP = "# ZD Ultimate Legend - Diagnostic Bundle"
HEADING_CLAIM_BOUNDARY = "## What this is (claim boundary)"
HEADING_HARDWARE = "## Hardware"
HEADING_MODULE_PASSPORTS = "## Module Passports"
HEADING_RECENT_HEALTH_REPORTS = "## Recent Health Reports"
HEADING_LIFECYCLE_SUMMARY = "## Lifecycle summary (Wear Ledger)"
HEADING_NOT_INCLUDED = "## What was NOT included"


def _utc_now_iso(now_fn: Callable[[], datetime]) -> str:
    return now_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DiagnosticBundleService:
    """Aggregates wrapper records into a shareable Markdown / ZIP bundle.

    Constructor dependencies are all optional (``None``-tolerant): callers
    that lack a wear ledger, module passport, or health-report directory
    still get a renderable bundle with graceful placeholders in the
    missing sections.

    The service does NOT auto-create the output directory. The caller
    chooses where the bundle is written via :meth:`generate_bundle_zip`'s
    ``target_path``; tests pass a temp dir, the UI integration writes into
    ``<user_data_dir>/diagnostic_bundles/``.
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        module_passport_service: Optional[ModulePassportService] = None,
        health_report_dir: Optional[Path] = None,
        wear_ledger: Optional[WearLedgerService] = None,
        app_data_dir: Optional[Path] = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._base_dir = Path(base_dir)
        self._module_passport_service = module_passport_service
        self._health_report_dir = (
            Path(health_report_dir) if health_report_dir is not None else None
        )
        self._wear_ledger = wear_ledger
        # ``app_data_dir`` is the wrapper's user-data-dir root (e.g.,
        # %APPDATA%/ZDUltimateLegend). Used by ``_sanitize_path`` to
        # rewrite any absolute path that sits inside the wrapper's own
        # data area with ``<APP_DATA>``. If not provided, sanitization
        # falls back to a name-based heuristic (look for the
        # ``ZDUltimateLegend`` segment in the path).
        self._app_data_dir = (
            Path(app_data_dir) if app_data_dir is not None else None
        )
        self._utc_now = utc_now
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception(
                "DiagnosticBundleService: failed to create base_dir %r",
                str(self._base_dir),
            )
        logger.info(
            "DiagnosticBundleService constructed: service_id=%s base_dir=%r",
            id(self),
            str(self._base_dir),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_markdown(
        self,
        *,
        include_archived: bool = True,
        health_report_limit: int = DEFAULT_HEALTH_REPORT_LIMIT,
        wear_ledger_days: int = DEFAULT_WEAR_LEDGER_DAYS,
        device_identity: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Render the Markdown report and return it as a string.

        ``device_identity`` is an optional mapping with controller-product /
        firmware / connection / active-slot fields the UI hands in from the
        live device service. When ``None``, the Hardware section renders
        "Unknown" placeholders rather than crashing.
        """

        sections = [
            self._render_header(),
            self._render_claim_boundary(),
            self._render_hardware(device_identity),
            self._render_module_passports(include_archived=include_archived),
            self._render_recent_health_reports(limit=health_report_limit),
            self._render_lifecycle_summary(wear_ledger_days=wear_ledger_days),
            self._render_not_included(),
        ]
        return "\n\n".join(s.rstrip() for s in sections if s) + "\n"

    def generate_bundle_zip(
        self,
        target_path: Path,
        *,
        include_archived: bool = True,
        health_report_limit: int = DEFAULT_HEALTH_REPORT_LIMIT,
        wear_ledger_days: int = DEFAULT_WEAR_LEDGER_DAYS,
        device_identity: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Path]:
        """Write a ZIP bundle to ``target_path``. Returns the path on success.

        Layout inside the ZIP::

            report.md
            module_passports/left.json                (if assigned)
            module_passports/right.json               (if assigned)
            module_passports/archive/*.json           (if include_archived)
            health_reports/<latest-N>.{md,json}       (best-effort siblings)
            wear_ledger_summary.json

        Returns ``None`` if the ZIP write failed; the Markdown render
        itself never raises (only its disk write can fail).
        """

        target_path = Path(target_path)
        markdown = self.generate_markdown(
            include_archived=include_archived,
            health_report_limit=health_report_limit,
            wear_ledger_days=wear_ledger_days,
            device_identity=device_identity,
        )

        wear_ledger_summary = self._wear_ledger_summary(
            wear_ledger_days=wear_ledger_days
        )

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                target_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as zf:
                zf.writestr("report.md", markdown)
                self._write_module_passports_to_zip(
                    zf, include_archived=include_archived
                )
                self._write_health_reports_to_zip(
                    zf, limit=health_report_limit
                )
                zf.writestr(
                    "wear_ledger_summary.json",
                    json.dumps(
                        wear_ledger_summary,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
        except (OSError, zipfile.BadZipFile, ValueError):
            logger.exception(
                "DiagnosticBundleService: ZIP write failed at %r",
                str(target_path),
            )
            return None

        return target_path

    def emit_generated_event(
        self,
        *,
        output_filename: str,
        bundle_format: str,
        include_archived: bool,
        health_report_limit: int,
        wear_ledger_days: int,
    ) -> None:
        """Record a wear-ledger event for a successful bundle generation.

        The output_filename is a basename (no path) so the ledger row
        doesn't leak the user's home path. Callers must extract the
        basename themselves; the service just records what it's given.
        """

        if self._wear_ledger is None:
            return
        summary = f"Diagnostic bundle generated ({bundle_format})"
        try:
            self._wear_ledger.append(
                DIAGNOSTIC_BUNDLE_GENERATED,
                summary=summary,
                details={
                    "format": bundle_format,
                    "output_filename": output_filename,
                    "include_archived": bool(include_archived),
                    "health_report_limit": int(health_report_limit),
                    "wear_ledger_days": int(wear_ledger_days),
                },
            )
        except Exception:  # noqa: BLE001 — wear ledger never crashes producer
            logger.exception(
                "DiagnosticBundleService: wear_ledger.append failed"
            )

    # ------------------------------------------------------------------
    # Path sanitization
    # ------------------------------------------------------------------

    def _sanitize_path(self, value: Any) -> str:
        """Rewrite absolute-path-shaped strings to a privacy-safe form.

        Delegates to the shared scrubber (:func:`scrub_value`): app-data
        paths collapse to ``<APP_DATA>/<tail>``, the account username after a
        ``Users``/``home`` root is dropped (even when it contains spaces — a
        display-name account like ``C:\\Users\\Jane Doe\\...``), and any other
        absolute path is reduced to its basename. ``None``/empty coerce to
        ``""``; other non-strings via ``str(...)``.
        """

        return scrub_value(value)

    # ------------------------------------------------------------------
    # Markdown rendering — individual sections
    # ------------------------------------------------------------------

    def _render_header(self) -> str:
        commit_label = __build_commit__ or "dev"
        return (
            f"{HEADING_TOP}\n"
            f"*Generated: {_utc_now_iso(self._utc_now)}*\n"
            f"*App version: {__version__} ({commit_label})*"
        )

    def _render_claim_boundary(self) -> str:
        return f"{HEADING_CLAIM_BOUNDARY}\n{CLAIM_BOUNDARY_PARAGRAPH}"

    def _render_hardware(self, device_identity: Optional[Mapping[str, Any]]) -> str:
        identity = dict(device_identity) if device_identity else {}
        # These come from the live device service and are not expected to carry
        # paths, but they flow into the shareable report — scrub defensively so
        # a future path-bearing caller can't leak a home path. ``_sanitize_path``
        # returns "" for falsy values, so the ``or "Unknown"`` fallback holds.
        product = self._sanitize_path(identity.get("product_string")) or "Unknown"
        firmware = self._sanitize_path(identity.get("firmware_version")) or "Unknown"
        connection = self._sanitize_path(identity.get("connection")) or "Unknown"
        slot = identity.get("active_slot")
        slot_label = f"Config {slot}" if slot is not None else "Unknown"
        lines = [
            HEADING_HARDWARE,
            f"- Controller product string: {product}",
            f"- Firmware: {firmware}",
            f"- Connection: {connection}",
            f"- Active onboard slot: {slot_label}",
        ]
        return "\n".join(lines)

    def _render_module_passports(self, *, include_archived: bool) -> str:
        lines: list[str] = [HEADING_MODULE_PASSPORTS]
        service = self._module_passport_service
        for side in SIDES:
            side_label = "Left" if side == SIDE_LEFT else "Right"
            lines.append(f"### {side_label}")
            passport = service.get(side) if service is not None else None
            if passport is None:
                lines.append("- Not assigned")
                lines.append("")
                continue
            lines.extend(self._render_passport_body(passport))
            lines.append("")

        lines.append("### Archived passports")
        if not include_archived or service is None:
            lines.append(
                "- Not included (set 'Include archived passports' to True to "
                "embed prior modules)"
                if not include_archived
                else "- No archived passports recorded"
            )
            return "\n".join(lines)

        archived: list[ModulePassport] = []
        for side in SIDES:
            try:
                archived.extend(service.list_archive(side))
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception(
                    "DiagnosticBundleService: list_archive(%s) raised", side
                )
        if not archived:
            lines.append("- No archived passports recorded")
            return "\n".join(lines)

        # Newest first (already sorted by service); group by side.
        for passport in archived:
            side_label = "Left" if passport.side == SIDE_LEFT else "Right"
            last_status = "no_runs"
            latest = passport.latest_fingerprint()
            if latest is not None:
                last_status = latest.overall_status
            lines.append(
                f"- {side_label}: {self._sanitize_path(passport.module_id)} "
                f"(assigned {passport.assigned_at_utc}; runs: "
                f"{len(passport.fingerprints)}; final status: {last_status})"
            )
        return "\n".join(lines)

    def _render_passport_body(self, passport: ModulePassport) -> list[str]:
        # module_id + notes are operator-assignable freeform text, so they can
        # carry absolute paths (usernames/home dirs). Run them through
        # _sanitize_path to uphold the module's "all paths sanitized" guarantee
        # in the shareable report.md.
        lines = [
            f"- Module ID: {self._sanitize_path(passport.module_id)}",
            f"- Assigned: {passport.assigned_at_utc}",
            f"- Notes: {self._sanitize_path(passport.notes) if passport.notes else '(none)'}",
        ]
        latest = passport.latest_fingerprint()
        if latest is None:
            lines.append("- Most recent characterization: (not yet run)")
            lines.append("- Characterization history: 0 fingerprints")
            return lines
        lines.append(
            f"- Most recent characterization "
            f"({latest.timestamp_utc}, {int(latest.duration_ms)} ms, "
            f"{int(latest.samples_count)} samples):"
        )
        lines.extend(self._render_fingerprint_metrics(latest))
        lines.append(
            f"- Characterization history: {len(passport.fingerprints)} "
            f"fingerprint(s) (oldest "
            f"{passport.fingerprints[0].timestamp_utc}, newest "
            f"{latest.timestamp_utc})"
        )
        return lines

    @staticmethod
    def _render_fingerprint_metrics(fp: ModuleFingerprint) -> list[str]:
        return [
            f"    - Noise floor: {fp.noise_floor_percent:.2f}%",
            (
                "    - Centering offset: "
                f"({fp.centering_offset_x:.2f}, {fp.centering_offset_y:.2f})%"
            ),
            (
                "    - Circularity coverage: "
                f"{fp.circularity_coverage_percent * 100:.1f}%"
            ),
            (
                "    - Outer deadzone: "
                f"{fp.outer_deadzone_min_axis:.2f}.."
                f"{fp.outer_deadzone_max_axis:.2f}"
            ),
            f"    - Asymmetry: {fp.asymmetry_score:.3f}",
            f"    - Bitness: {int(fp.bitness_observed)}",
            f"    - Tremor: {fp.tremor_metric:.3f}",
            f"    - Linearity: {fp.linearity_score:.3f}",
            f"    - Overall: {fp.overall_status}",
        ]

    def _render_recent_health_reports(self, *, limit: int) -> str:
        lines = [HEADING_RECENT_HEALTH_REPORTS]
        files = self._list_recent_health_reports(limit=limit)
        if not files:
            lines.append("- No exports found")
            return "\n".join(lines)
        lines.append(
            f"- Including {len(files)} most-recent export(s) "
            f"(full file(s) bundled under `health_reports/` when ZIP "
            "format is used)."
        )
        for path in files:
            summary = self._summarize_health_report(path)
            lines.append(f"- `{summary['filename']}`")
            for line in summary["lines"]:
                lines.append(f"    - {line}")
        return "\n".join(lines)

    def _render_lifecycle_summary(self, *, wear_ledger_days: int) -> str:
        lines = [
            f"{HEADING_LIFECYCLE_SUMMARY}, last {wear_ledger_days} days"
        ]
        summary = self._wear_ledger_summary(wear_ledger_days=wear_ledger_days)
        counts = summary["counts"]
        lines.append(f"- Total sessions: {counts.get('session_start', 0)}")
        lines.append(f"- Total profile applies: {counts.get('profile_apply', 0)}")
        lines.append(f"- Total RP captures: {counts.get('rp_capture', 0)}")
        lines.append(f"- Total RP restores: {counts.get('rp_restore', 0)}")
        lines.append(f"- Total Health Reports: {counts.get('health_report', 0)}")
        lines.append(
            f"- Total Readiness Checks: {counts.get('readiness_check', 0)}"
        )
        lines.append(
            "- Total module assignments: "
            f"{counts.get('module_assigned', 0)}"
        )
        lines.append(
            "- Total module characterizations: "
            f"{counts.get('module_characterized', 0)}"
        )
        lines.append(
            "- Total module-note edits: "
            f"{counts.get('module_notes_updated', 0)}"
        )
        lines.append(
            f"- Total service notes: {counts.get('service_note', 0)}"
        )
        lines.append(
            "- Total prior diagnostic bundles: "
            f"{counts.get('diagnostic_bundle_generated', 0)}"
        )
        recent_notes = summary["recent_service_notes"]
        lines.append("- Recent service notes:")
        if not recent_notes:
            lines.append("    - (none in window)")
        else:
            for entry in recent_notes:
                preview = entry.get("preview", "")
                ts = entry.get("ts", "")
                lines.append(f"    - {ts}: {preview}")
        return "\n".join(lines)

    def _render_not_included(self) -> str:
        return (
            f"{HEADING_NOT_INCLUDED}\n"
            "- Full event-timing log (privacy; only roll-up counts above)\n"
            "- IP addresses or network info (the wrapper has none to begin with)\n"
            "- User home path beyond the "
            f"`{APP_DATA_PLACEHOLDER}` placeholder above\n"
            "- Other Windows processes or system telemetry "
            "(the wrapper does not collect any)"
        )

    # ------------------------------------------------------------------
    # Health-Report directory helpers
    # ------------------------------------------------------------------

    def _list_recent_health_reports(self, *, limit: int) -> list[Path]:
        """Return paths to the most-recent N exported Health Report files.

        We sort by modification time descending and prefer ``.md`` files
        when both ``.md`` and ``.json`` exist for the same stamp (the UI
        renders the Markdown summary above; the ZIP packs both siblings).
        Returns an empty list when the directory is missing or unreadable.
        """

        directory = self._health_report_dir
        if directory is None or not directory.exists():
            return []
        try:
            # The Health Report export writes
            # ``zd_health_report_<YYYY-MM-DD_HHMMSS>.{md,json}`` (see
            # ``ui/screens/health_report.py:_save_report``), so a
            # lexicographic reverse-name sort equals chronological
            # newest-first. We deliberately don't sort by ``mtime`` —
            # mtimes get re-stamped by copies / restores / git checkouts
            # in ways the filename timestamp doesn't, so the filename is
            # the more reliable source of truth.
            md_files = sorted(
                directory.glob("*.md"),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            logger.exception(
                "DiagnosticBundleService: failed to glob %r", str(directory)
            )
            return []
        return md_files[: max(0, int(limit))]

    def _summarize_health_report(self, path: Path) -> dict[str, Any]:
        """Build a 1-line-per-key summary for the Markdown bundle.

        Looks for the JSON sibling (same stem) and pulls a small subset of
        fields out of it. Falls back to "(JSON sibling not found)" lines
        when the JSON twin is missing or malformed.
        """

        result: dict[str, Any] = {
            "filename": path.name,
            "lines": [],
        }
        json_sibling = path.with_suffix(".json")
        if not json_sibling.exists():
            result["lines"] = [
                "(JSON sibling not found — Markdown-only export)",
            ]
            return result
        try:
            payload = json.loads(json_sibling.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception(
                "DiagnosticBundleService: failed to read health report JSON %r",
                str(json_sibling),
            )
            result["lines"] = ["(JSON sibling unreadable)"]
            return result
        if not isinstance(payload, Mapping):
            result["lines"] = ["(JSON sibling not a mapping)"]
            return result
        overall = payload.get("overall_status") or payload.get("verdict") or "unknown"
        captured_at = payload.get("captured_at_utc") or payload.get("timestamp") or ""
        sample_count = payload.get("sample_count")
        device = payload.get("device") or {}
        controller_name = "unknown"
        if isinstance(device, Mapping):
            controller_name = (
                device.get("controller_name") or device.get("product_string") or "unknown"
            )
        # Echoed JSON values are scrubbed: a health-report artifact could carry
        # an absolute path (e.g. in the device/controller string), and report.md
        # ships in the same shareable bundle.
        result["lines"] = [
            f"Captured: {self._sanitize_path(captured_at)}",
            f"Controller: {self._sanitize_path(controller_name)}",
            f"Overall: {self._sanitize_path(overall)}",
        ]
        if sample_count is not None:
            try:
                result["lines"].append(f"Samples: {int(sample_count)}")
            except (TypeError, ValueError):
                pass
        return result

    # ------------------------------------------------------------------
    # Wear-ledger summary
    # ------------------------------------------------------------------

    def _wear_ledger_summary(self, *, wear_ledger_days: int) -> dict[str, Any]:
        """Build a privacy-conservative roll-up of the wear-ledger window.

        Returned shape::

            {
              "window_days": <int>,
              "counts": { event_type: count, ... },
              "rp_restore_labels": { label: count, ... },
              "readiness_check_verdicts": { verdict: count, ... },
              "module_characterized_verdicts": { verdict: count, ... },
              "recent_service_notes": [
                  {"ts": "...", "preview": "...first 80 chars..."}, ...
              ],
            }

        Does NOT include full event timing or raw summary lines.
        """

        empty = {
            "window_days": int(wear_ledger_days),
            "counts": {},
            "rp_restore_labels": {},
            "readiness_check_verdicts": {},
            "module_characterized_verdicts": {},
            "recent_service_notes": [],
        }
        if self._wear_ledger is None:
            return empty
        try:
            since = self._utc_now() - _timedelta_days(wear_ledger_days)
            events = self._wear_ledger.read_events(since=since)
        except Exception:  # noqa: BLE001
            logger.exception(
                "DiagnosticBundleService: read_events failed"
            )
            return empty

        counts: dict[str, int] = {}
        rp_restore_labels: dict[str, int] = {}
        readiness_check_verdicts: dict[str, int] = {}
        module_characterized_verdicts: dict[str, int] = {}
        recent_service_notes: list[dict[str, str]] = []
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            details = event.details or {}
            if event.event_type == RP_RESTORE:
                label = str(details.get("result_label") or details.get("label") or "unknown")
                rp_restore_labels[label] = rp_restore_labels.get(label, 0) + 1
            elif event.event_type == READINESS_CHECK:
                verdict = str(details.get("verdict") or "unknown")
                readiness_check_verdicts[verdict] = (
                    readiness_check_verdicts.get(verdict, 0) + 1
                )
            elif event.event_type == MODULE_CHARACTERIZED:
                verdict = str(details.get("overall_status") or "unknown")
                module_characterized_verdicts[verdict] = (
                    module_characterized_verdicts.get(verdict, 0) + 1
                )
            elif event.event_type == SERVICE_NOTE:
                if len(recent_service_notes) < DEFAULT_RECENT_SERVICE_NOTES:
                    preview = str(details.get("note") or event.summary or "")
                    preview = preview.strip().replace("\n", " ")
                    if len(preview) > 80:
                        preview = preview[:80] + "..."
                    recent_service_notes.append(
                        {
                            "ts": self._sanitize_path(event.ts),
                            "preview": self._sanitize_path(preview),
                        }
                    )
        return {
            "window_days": int(wear_ledger_days),
            "counts": counts,
            "rp_restore_labels": rp_restore_labels,
            "readiness_check_verdicts": readiness_check_verdicts,
            "module_characterized_verdicts": module_characterized_verdicts,
            "recent_service_notes": recent_service_notes,
        }

    # ------------------------------------------------------------------
    # ZIP helpers
    # ------------------------------------------------------------------

    def _sanitized_passport_dict(self, passport: ModulePassport) -> dict[str, Any]:
        """``passport.to_dict()`` with the operator-assignable freeform fields
        (``module_id`` + ``notes``) run through :meth:`_sanitize_path`.

        These are the only passport fields that can carry an absolute path
        (the rest are timestamps / numeric metrics), so scrubbing them keeps
        the JSON dump inside the bundle consistent with report.md and the
        module's "all paths sanitized" guarantee.
        """

        raw = passport.to_dict()
        raw["module_id"] = self._sanitize_path(raw.get("module_id"))
        raw["notes"] = self._sanitize_path(raw.get("notes"))
        return raw

    def _write_module_passports_to_zip(
        self,
        zf: zipfile.ZipFile,
        *,
        include_archived: bool,
    ) -> None:
        service = self._module_passport_service
        if service is None:
            return
        for side in SIDES:
            try:
                passport = service.get(side)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "DiagnosticBundleService: passport get(%s) raised", side
                )
                continue
            if passport is None:
                continue
            try:
                payload = json.dumps(
                    self._sanitized_passport_dict(passport),
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                zf.writestr(f"module_passports/{side}.json", payload)
            except (TypeError, ValueError):
                logger.exception(
                    "DiagnosticBundleService: failed to serialize passport (%s)",
                    side,
                )
        if not include_archived:
            return
        for side in SIDES:
            try:
                archived = service.list_archive(side)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "DiagnosticBundleService: list_archive(%s) raised", side
                )
                continue
            for idx, passport in enumerate(archived):
                try:
                    payload = json.dumps(
                        self._sanitized_passport_dict(passport),
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    # Stable, sortable archive filename — uses the passport's
                    # own metadata so the entry order matches the rendered
                    # order in the Markdown.
                    safe_id = _safe_filename_segment(self._sanitize_path(passport.module_id))
                    stamp = _safe_filename_segment(passport.assigned_at_utc)
                    zf.writestr(
                        (
                            f"module_passports/archive/"
                            f"{side}_{safe_id}_{stamp}_{idx:03d}.json"
                        ),
                        payload,
                    )
                except (TypeError, ValueError):
                    logger.exception(
                        "DiagnosticBundleService: failed to serialize "
                        "archived passport (%s, idx=%d)",
                        side,
                        idx,
                    )

    def _write_health_reports_to_zip(
        self,
        zf: zipfile.ZipFile,
        *,
        limit: int,
    ) -> None:
        files = self._list_recent_health_reports(limit=limit)
        for md_path in files:
            try:
                zf.writestr(
                    f"health_reports/{md_path.name}",
                    self._sanitize_path(md_path.read_text(encoding="utf-8")),
                )
            except OSError:
                logger.exception(
                    "DiagnosticBundleService: failed to read MD %r",
                    str(md_path),
                )
                continue
            json_sibling = md_path.with_suffix(".json")
            if json_sibling.exists():
                try:
                    zf.writestr(
                        f"health_reports/{json_sibling.name}",
                        self._sanitize_path(json_sibling.read_text(encoding="utf-8")),
                    )
                except OSError:
                    logger.exception(
                        "DiagnosticBundleService: failed to read JSON %r",
                        str(json_sibling),
                    )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_segment(value: str) -> str:
    cleaned = _FILENAME_SAFE_RE.sub("_", str(value)).strip("_")
    return cleaned or "unknown"


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=max(0, int(days)))


__all__ = [
    "APP_DATA_PLACEHOLDER",
    "BUNDLE_DIRNAME",
    "DEFAULT_HEALTH_REPORT_LIMIT",
    "DEFAULT_WEAR_LEDGER_DAYS",
    "DiagnosticBundleService",
]
