"""Manual diagnostic: per-card content-clip probe (REAL screens, shipped fonts).

Sibling to ``tools/diag_dpg_content_scrollbar.py``. That bench answers "how many
scrollbars does each screen have?" at the AppShell wrapper layer. This bench
answers the finer question the card-clip lane needs: **which individual cards
(child_windows) clip their own content and grow an INNER scrollbar** when
rendered at the SHIPPED fonts (Inter 14 body / Inter SemiBold 18 section titles,
Noto Sans SC for zh-CN) with realistic content.

Why a dedicated bench: a card built as ``dpg.child_window(height=<fixed px>)``
silently clips when its real rendered content exceeds that hardcoded height —
DPG turns the overflow into an inner vertical scrollbar. Headless/source
reasoning about "this height was measured to fit" is unreliable (the Live Verify
cards carried exactly such comments and STILL clipped on the operator's machine),
so we render on a real viewport with the real global theme + the real bundled
fonts and MEASURE every card.

The signal per child_window is ``dpg.get_y_scroll_max(item)``: the maximum
vertical scroll offset (``content_height - visible_height`` clamped at 0).
``> 0.5`` => the card clips and there is hidden content below the fold; that
value is ALSO the overshoot in pixels (how much taller the content is than the
card). ``<= 0.5`` => the content fits, no clip. We walk EVERY child_window under
each screen root (recursively, through groups/tables) plus the modal-only Safe
Import preview, and report each clipper with its tag, fixed height, rendered
size, overshoot px, and a text sample for identification.

CRITICAL — fonts: the bundled Inter / Noto Sans SC faces render TALLER than Dear
PyGui's built-in proggy face. We MUST ``register_fonts()`` + ``bind_default_font``
exactly like ``AppShell.run`` (app_shell.py:1258-1260) or every card under-reports
its true height. The content-scrollbar bench skips font registration (it only
cares about wrapper-level scroll counts); this one cannot.

CRITICAL — seed realistic content: clips are content-dependent. Empty mocks
under-report. We attach REAL temp-dir-backed services seeded with a module
assigned (passport + buttons), many wear-ledger events, and many restore points,
and drive content-heavy screen states (the modules compare view, etc.).

CRITICAL — drive the EXPANDED / verdict states: a card built tall enough for its
COLLAPSED form still clips when EXPANDED. The original card-clip lane's probe
only ever measured collapsed/intro states, so it missed (a) the modules
expanded-fingerprint rows (the ``60 + 24*8`` magic-height rows at modules.py:716
and :972 — they overflow when a fingerprint's 8-metric detail is open) and (b)
the readiness-check DONE verdict card (only rendered in the COMPLETE state). This
probe forces both: an active + archived passport with a degrading fingerprint arc
(also trips the trend "investigate" roll-up so the attention banner + per-metric
trend rows render) and a RED 4-observation readiness verdict.

Run ONE (locale, size) per process — Dear PyGui is happiest with a single
context per process and a single locale per font binding. A small matrix driver
loops over locales/sizes by re-invoking this script.

Usage (system py312 — agent venvs lack dearpygui):
    py312 tools/diag_dpg_card_clip.py                       # en, 1480x920, all screens
    py312 tools/diag_dpg_card_clip.py --locale zh-CN
    py312 tools/diag_dpg_card_clip.py --width 1180 --height 760
    py312 tools/diag_dpg_card_clip.py --screen modules
    py312 tools/diag_dpg_card_clip.py --matrix             # spawn the full locale x size matrix
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Make the repo root importable so ``tests`` + ``zd_app`` resolve when this is
# launched as a bare script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The samples include CJK + status glyphs (■) that crash the default cp1252
# Windows console encoder. Force UTF-8 with replacement so the matrix prints.
try:  # pragma: no cover - environment dependent
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Public navigation screens that build a per-screen root under content_region.
# (live_verify is excluded — it is already fixed and runs an XInput poll worker
# we do not want to spin up headless; settings/about are short but cheap to walk.)
SCREENS = (
    "home",
    "controller",
    "diagnostics",
    "readiness_check",
    "restore_points",
    "device_vs_profile",
    "health_report",
    "wear_ledger",
    "modules",
    "settings",
    "about",
)
# Extra modules sub-views worth measuring (compare card is a known fixed-height
# suspect). Keyed by the ModulesScreenState.view to force before rendering.
SETTLE_FRAMES = 45  # autosize layout needs several passes to converge

RESULT_PREFIX = "RESULT"
CLIP_THRESHOLD = 0.5  # px of scroll-max that counts as a real clip

# Deliberate bounded scroll regions: a fixed-height card that is SUPPOSED to
# scroll a long list within a bounded area (not a content-clip bug). The
# restore-points list and wear-ledger event log scroll via the legacy
# ``autosize_y`` fill (caught by the autosize_y rule below); the Safe Import diff
# list is a fixed-height scroll region inside a fixed-height modal, so it is
# allowlisted by tag.
INTENTIONAL_SCROLL_TAGS = {"safe_import_diff_region"}


# ---------------------------------------------------------------------------
# Card walking + measurement
# ---------------------------------------------------------------------------


def _is_child_window(dpg, item) -> bool:
    try:
        return dpg.get_item_type(item).endswith("mvChildWindow")
    except Exception:
        return False


def _sample_texts(dpg, item, limit: int = 2) -> list[str]:
    """Collect the first few non-empty text values under ``item`` (for ID)."""

    out: list[str] = []

    def _descend(node) -> None:
        if len(out) >= limit:
            return
        try:
            kids = dpg.get_item_children(node, 1) or []
        except Exception:
            return
        for kid in kids:
            if len(out) >= limit:
                return
            try:
                ktype = dpg.get_item_type(kid)
            except Exception:
                continue
            if ktype.endswith("mvText"):
                try:
                    val = dpg.get_value(kid)
                except Exception:
                    val = None
                if val:
                    s = str(val).strip().replace("\n", " ")
                    if s:
                        out.append(s[:48])
            _descend(kid)

    _descend(item)
    return out


def _walk_cards(dpg, root, path=()) -> list[dict]:
    """Recursively collect every child_window under ``root`` with its metrics."""

    results: list[dict] = []
    try:
        kids = dpg.get_item_children(root, 1) or []
    except Exception:
        kids = []
    for idx, kid in enumerate(kids):
        kid_path = path + (idx,)
        if _is_child_window(dpg, kid):
            try:
                ysm = float(dpg.get_y_scroll_max(kid))
            except Exception:
                ysm = 0.0
            try:
                xsm = float(dpg.get_x_scroll_max(kid))
            except Exception:
                xsm = 0.0
            try:
                cfg = dpg.get_item_configuration(kid)
            except Exception:
                cfg = {}
            try:
                rect = list(dpg.get_item_rect_size(kid))
            except Exception:
                rect = [0, 0]
            try:
                alias = dpg.get_item_alias(kid) or ""
            except Exception:
                alias = ""
            results.append(
                {
                    "item": kid,
                    "tag": alias,
                    "path": kid_path,
                    "y_scroll_max": ysm,
                    "x_scroll_max": xsm,
                    "cfg_height": cfg.get("height"),
                    "cfg_width": cfg.get("width"),
                    "autosize_y": cfg.get("autosize_y"),
                    "auto_resize_y": cfg.get("auto_resize_y"),
                    "rect": rect,
                    "sample": _sample_texts(dpg, kid),
                }
            )
        # Descend into ALL containers (groups/tables/child windows) so nested
        # cards are reached.
        results.extend(_walk_cards(dpg, kid, kid_path))
    return results


def _scrolls(c: dict) -> bool:
    return (
        c.get("y_scroll_max", 0.0) > CLIP_THRESHOLD
        or c.get("x_scroll_max", 0.0) > CLIP_THRESHOLD
    )


def _clip_kind(c: dict) -> str | None:
    """Classify a scrolling card: 'real' (a bug) vs 'intentional' vs None.

    A real clip is a FIXED-size card whose content overflowed. A card that
    scrolls via the legacy ``autosize_y`` fill, or one allowlisted by tag, is a
    deliberate bounded scroll region (a long list inside a bounded area), not a
    content-clip bug.
    """

    if not _scrolls(c):
        return None
    if c.get("autosize_y") is True:
        return "intentional"
    # A negative fixed height (height=-N) is the footer-reserve fill pattern
    # (components.card with height<0): fill the parent's remaining height MINUS N
    # px and scroll the contents INTERNALLY on overflow — a deliberate bounded
    # scroll surface (the restore-points list / FOOTER_RESERVE single-scroll),
    # not a content-clip bug. Classify it like autosize_y. A REAL fixed-card clip
    # always has height > 0. (Before the seeding fix this never surfaced because
    # the restore-points list under-seeded to empty and never overflowed.)
    cfg_h = c.get("cfg_height")
    if isinstance(cfg_h, (int, float)) and cfg_h < 0:
        return "intentional"
    if c.get("tag") in INTENTIONAL_SCROLL_TAGS:
        return "intentional"
    return "real"


def _is_clip(c: dict) -> bool:
    return _clip_kind(c) == "real"


def _format_card(c: dict) -> str:
    tag = c["tag"] or f"path{list(c['path'])}"
    h = c["cfg_height"]
    w = c.get("cfg_width")
    rect = c["rect"]
    sample = " | ".join(c["sample"]) if c["sample"] else "(no text)"
    axis = []
    if c.get("y_scroll_max", 0.0) > CLIP_THRESHOLD:
        axis.append(f"V={c['y_scroll_max']:.0f}px")
    if c.get("x_scroll_max", 0.0) > CLIP_THRESHOLD:
        axis.append(f"H={c['x_scroll_max']:.0f}px")
    return (
        f"overshoot[{','.join(axis)}]  fixed=({w}x{h})  rect={rect}  "
        f"autosize_y={c['autosize_y']} auto_resize_y={c['auto_resize_y']}  "
        f"tag={tag}\n          text: {sample}"
    )


# ---------------------------------------------------------------------------
# Realistic content seeding
# ---------------------------------------------------------------------------


def _seed_services(shell, tmp: Path) -> None:
    """Attach REAL temp-dir-backed services seeded with heavy, realistic data."""

    # --- Device-service formatted values (Home connection / health cards) ----
    # make_shell leaves format_firmware_version()/format_battery_level()
    # unmocked, so they return long MagicMock reprs that the real app never
    # shows (the real values are short like "1.18" / "85%"). Seed short strings
    # so the probe doesn't false-positive a horizontal clip on those metrics.
    try:
        shell.device_service.format_firmware_version.return_value = "1.18"
        shell.device_service.format_battery_level.return_value = "85%"
        shell.device_service.state.product_name = "ZD Ultimate Legend"
        shell.device_service.state.connection_state = "connected"
        shell.device_service.state.connection_mode = "USB"
        shell.device_service.state.last_read_time = "2026-05-26T18:42:00"
    except Exception:
        pass

    # --- Device-service recent events (Home recent-activity card) ------------
    # home._recent_activity reads device_service.recent_events(10); the make_shell
    # mock returns [] which UNDER-reports that card. Seed 10 realistic lines.
    try:
        shell.device_service.recent_events.return_value = [
            "2026-05-26 18:42  Read controller state (USB, 8 fields)",
            "2026-05-26 18:40  Applied racing profile (12 fields written)",
            "2026-05-26 18:35  Captured restore point before firmware update",
            "2026-05-26 18:30  Health check: tuning suggested (left stick drift)",
            "2026-05-26 18:22  Readiness check passed (6 fields verified)",
            "2026-05-26 18:15  Restored to racing profile (8 fields)",
            "2026-05-26 18:09  Sensitivity slider updated to 75",
            "2026-05-26 18:01  Applied accessibility layout (10 fields written)",
            "2026-05-26 17:55  Health check: warning (trigger feel)",
            "2026-05-26 17:48  Captured restore point before trigger calibration",
        ]
    except Exception:
        pass

    # --- Wear ledger (also feeds Home recent-activity + Modules trend) -------
    try:
        from zd_app.services.wear_ledger import WearLedgerService
        from zd_app.services.wear_ledger import models as wl

        ledger = WearLedgerService(base_dir=tmp / "ledger")
        kinds = [
            (wl.SESSION_START, "Session started"),
            (wl.PROFILE_APPLY, "Racing profile applied (12 fields written)"),
            (wl.RP_CAPTURE, "Manual backup before firmware update"),
            (wl.HEALTH_REPORT, "Health check: tuning suggested"),
            (wl.READINESS_CHECK, "Pre-play readiness check: pass"),
            (wl.RP_RESTORE, "Restored to racing profile"),
            (wl.SLIDER_WRITE, "Sensitivity slider updated to 75"),
            (wl.PROFILE_APPLY, "Accessibility layout applied"),
            (wl.HEALTH_REPORT, "Health check: warning (left stick drift)"),
            (wl.RP_CAPTURE, "Backup before trigger calibration"),
        ]
        for kind, summary in kinds:
            try:
                ledger.append(kind, summary=summary)
            except Exception:
                pass
        shell.wear_ledger_service = ledger
    except Exception as exc:  # pragma: no cover - diagnostic resilience
        print(f"  [seed] wear ledger skipped: {exc}", flush=True)
        ledger = None

    # --- Module passport (both sides assigned, with fingerprints) ------------
    try:
        from zd_app.services.module_passport import ModulePassportService
        from zd_app.storage.module_passport_models import (
            ModuleFingerprint,
            SIDE_LEFT,
            SIDE_RIGHT,
            STATUS_GOOD,
            STATUS_WATCH,
        )

        def _fp(
            side,
            status,
            ts,
            *,
            noise_floor: float = 1.4,
            tremor: float = 0.2,
            centering_x: float = 0.2,
        ) -> ModuleFingerprint:
            return ModuleFingerprint(
                timestamp_utc=ts,
                side=side,
                duration_ms=60_000,
                samples_count=12_000,
                noise_floor_percent=noise_floor,
                centering_offset_x=centering_x,
                centering_offset_y=-0.1,
                circularity_coverage_percent=0.97,
                outer_deadzone_min_axis=94.0,
                outer_deadzone_max_axis=98.0,
                asymmetry_score=0.05,
                bitness_observed=128,
                tremor_metric=tremor,
                linearity_score=0.04,
                overall_status=status,
                notes=None,
            )

        passports = ModulePassportService(base_dir=tmp / "passports", wear_ledger=ledger)

        # LEFT — seed an ARCHIVED passport first (assign + fingerprints), then
        # re-assign the active module so the prior one is archived WITH its
        # fingerprints. That lets the archived-LIST row (modules.py:841) and the
        # archived-detail EXPANDED fingerprint row (modules.py:972) render.
        passports.assign(
            SIDE_LEFT, "ARCHIVED_LEFT_V1", notes="prior left module - retired after drift"
        )
        for ts in ("2026-03-30T09:00:00Z", "2026-04-09T09:00:00Z", "2026-04-19T09:00:00Z"):
            passports.append_fingerprint(SIDE_LEFT, _fp(SIDE_LEFT, STATUS_GOOD, ts))

        # Active LEFT — a perfectly-linear degrading arc (5 points over 28 days)
        # toward each metric's watch threshold (noise_floor 6.0 / tremor 1.5 /
        # centering 6.0), runway < the 30-day INVESTIGATE horizon. Trips the trend
        # "investigate" roll-up on three metrics so the attention banner
        # (modules.py:637) names all three and every per-metric trend row
        # (modules.py:1390) renders its projection line — the tall variant.
        passports.assign(SIDE_LEFT, "STOCK_LEFT", notes="baseline left module")
        _LEFT_ARC = (
            # (timestamp, noise_floor, tremor, centering_x)
            ("2026-05-01T18:42:11Z", 3.0, 0.30, 2.00),
            ("2026-05-08T18:42:11Z", 3.7, 0.58, 2.95),
            ("2026-05-15T18:42:11Z", 4.4, 0.86, 3.90),
            ("2026-05-22T18:42:11Z", 5.1, 1.14, 4.85),
            ("2026-05-29T18:42:11Z", 5.8, 1.42, 5.70),
        )
        for ts, nf, tr, cx in _LEFT_ARC:
            status = STATUS_GOOD if nf < 4.5 else STATUS_WATCH
            passports.append_fingerprint(
                SIDE_LEFT,
                _fp(SIDE_LEFT, status, ts, noise_floor=nf, tremor=tr, centering_x=cx),
            )

        passports.assign(SIDE_RIGHT, "KSILVER_RIGHT", notes="aftermarket right module")
        passports.append_fingerprint(SIDE_RIGHT, _fp(SIDE_RIGHT, STATUS_GOOD, "2026-05-22T11:10:00Z"))
        shell.module_passport_service = passports
    except Exception as exc:  # pragma: no cover - diagnostic resilience
        print(f"  [seed] module passport skipped: {exc}", flush=True)

    # --- Restore points (many entries) ---------------------------------------
    try:
        _seed_restore_points(shell, tmp)
    except Exception as exc:  # pragma: no cover - diagnostic resilience
        print(f"  [seed] restore points skipped: {exc}", flush=True)


def _seed_restore_points(shell, tmp: Path) -> None:
    """Best-effort: seed ~10 restore points via the real service + store.

    Built defensively — if the model/constructor signatures differ from what we
    expect, we log and leave the screen in its empty state rather than crash the
    whole probe (restore_points list is a native table, not a primary suspect).
    """

    from unittest.mock import MagicMock

    from zd_app.services.restore_point_service import RestorePointService
    from zd_app.storage.restore_point_store import RestorePointStore

    store = RestorePointStore(str(tmp / "restore_points"))
    service = RestorePointService(
        store=store,
        settings_service=MagicMock(),
        apply_coordinator=MagicMock(),
        app_version="2.0.0",
        app_build_commit="probe",
    )
    # Prefer the store's own list/save round-trip via a real capture if exposed;
    # otherwise fall back to hand-built models.
    seeded = False
    try:
        from zd_app.storage.restore_point_models import (
            CaptureSource,
            CoverageCategory,
            CoverageState,
            DeviceIdentity,
            FieldCoverage,
            IdentityConfidence,
            KIND,
            RestorePoint,
            RestorePointCoverage,
            RestorePointTrigger,
            SCHEMA_VERSION,
        )
        from zd_app.services.settings_service import ControllerSnapshot

        coverage = RestorePointCoverage(
            captured_supported_count=8,
            total_supported_count=13,
            capture_source=CaptureSource.FRESH_READ,
            fields={
                "polling_rate": FieldCoverage(
                    state=CoverageState.CAPTURED,
                    readable=True,
                    writable=True,
                    category=CoverageCategory.DEVICE,
                ),
            },
        )
        ident = DeviceIdentity(
            vid="413D",
            pid="2104",
            product_string="ZD Ultimate Legend",
            firmware_version="1.18",
            identity_confidence=IdentityConfidence.READABLE,
        )
        titles = [
            "Before Safe Import — 2026-05-24 19:03",
            "Manual backup — tuned deadzones",
            "Post-firmware update 1.18",
            "Before macro remapping session",
            "Stock configuration baseline",
            "Pre-sensitivity adjustment",
            "Racing pad profile locked in",
            "After module swap (left K-Silver)",
            "Backup before trigger calibration",
            "Clean slate — factory defaults",
        ]
        for i, title in enumerate(titles):
            # Canonical id shape is enforced at LOAD time by
            # restore_point_models._RESTORE_POINT_ID_RE:
            # ``rp_YYYYMMDD_HHMMSS_<6 lowercase hex>``. The old probe minted
            # ``rp_<2d>_<6d>`` (e.g. rp_09_009000), which list() now rejects as
            # "invalid restore point id" — every seeded point was skipped, so the
            # restore_points + device_vs_profile screens silently under-seeded.
            # Build a valid id (and a matching created_at) from a distinct
            # day/hour per entry so all 10 points round-trip through save()->list().
            day = 20 + i          # 20..29 (valid May days, distinct per entry)
            hour = 10 + (i % 9)   # 10..18 (valid hours)
            created_at = f"2026-05-{day:02d}T{hour:02d}:00:00Z"
            rp = RestorePoint(
                schema_version=SCHEMA_VERSION,
                kind=KIND,
                id=f"rp_202605{day:02d}_{hour:02d}0000_{i:06x}",
                created_at=created_at,
                app_version="2.0.0",
                app_build_commit=None,
                title=title,
                trigger=RestorePointTrigger(
                    type="manual", source_label="User", reason=f"Restore point {i + 1}"
                ),
                device_identity=ident,
                snapshot=ControllerSnapshot(
                    polling_rate=None,
                    vibration=None,
                    deadzones=None,
                    axis_inversion_left=None,
                    axis_inversion_right=None,
                    sensitivity_left=None,
                    sensitivity_right=None,
                    trigger_left=None,
                    trigger_right=None,
                    button_bindings={},
                    lighting_zones={},
                    motion_settings=None,
                    back_paddle_bindings={},
                ),
                coverage=coverage,
                last_restore_attempt=None,
            )
            store.save(rp)
        seeded = True
    except Exception as exc:
        print(f"  [seed] restore-point models differ ({exc}); empty list", flush=True)

    if seeded:
        shell.restore_point_service = service


def _force_modules_compare(shell) -> None:
    """Put the Modules screen into its COMPARE view (the 360px compare card)."""

    try:
        from zd_app.ui.screens import modules as m

        state = m._ensure_state(shell)
        state.view = m.VIEW_COMPARE
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] modules compare view skipped: {exc}", flush=True)


def _force_restore_detail(shell) -> None:
    """Put Restore Points into its DETAIL view (the 160px detail card)."""

    try:
        from zd_app.ui.screens import restore_points as rp

        state = rp._ensure_state(shell)
        state.view = rp.VIEW_DETAIL
        # First seeded point id (see _seed_restore_points — i=0 mints
        # rp_202605{20}_{10}0000_000000); harmless if absent — the detail builder
        # degrades to the list when the id can't be loaded.
        state.selected_rp_id = "rp_20260520_100000_000000"
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] restore detail view skipped: {exc}", flush=True)


def _force_restore_result(shell) -> None:
    """Put Restore Points into its RESULT view (the 160px counts card)."""

    try:
        from zd_app.ui.screens import restore_points as rp
        from zd_app.storage.restore_point_models import (
            RestoreFieldOutcome,
            RestoreResult,
            RestoreResultLabel,
        )

        state = rp._ensure_state(shell)
        state.view = rp.VIEW_RESULT
        state.result = RestoreResult(
            label=RestoreResultLabel.VERIFIED,
            attempted=12,
            wrote_succeeded=11,
            write_failed=1,
            verified_matched=10,
            could_not_verify=1,
            mismatched=0,
            fields=tuple(
                RestoreFieldOutcome(
                    field_name=f"field_{i}",
                    write_succeeded=True,
                    write_error=None,
                    verify_matched=True,
                )
                for i in range(12)
            ),
            before_restore_point_id="rp_20260520_100000_000000",
            completed_at="2026-05-24T19:05:30Z",
        )
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] restore result view skipped: {exc}", flush=True)


def _force_modules_detail_expanded(shell) -> None:
    """Modules DETAIL view (LEFT) with the NEWEST fingerprint EXPANDED.

    Renders the active-detail expanded fingerprint card (modules.py:716 — the
    ``60 + 24*8`` magic-height row whose 8-metric detail overflows when open) and
    the trend attention banner (modules.py:637) once the seeded degrading arc
    trips the investigate roll-up. This is the state the original card-clip lane's
    probe never reached, so the clip was missed.
    """

    try:
        from zd_app.storage.module_passport_models import SIDE_LEFT
        from zd_app.ui.screens import modules as m

        state = m._ensure_state(shell)
        state.view = m.VIEW_DETAIL
        state.detail_side = SIDE_LEFT
        # _build_detail renders newest-first (idx counts down from len-1); the
        # expanded flag matches by idx, so expand the newest (idx == len-1).
        passport = shell.module_passport_service.get(SIDE_LEFT)
        n = len(passport.fingerprints) if passport else 0
        state.expanded_fingerprint_idx = (n - 1) if n else None
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] modules detail-expanded skipped: {exc}", flush=True)


def _force_modules_archived_list(shell) -> None:
    """Modules ARCHIVED-LIST view (LEFT) — the 130px archived entry rows (:841)."""

    try:
        from zd_app.storage.module_passport_models import SIDE_LEFT
        from zd_app.ui.screens import modules as m

        state = m._ensure_state(shell)
        state.view = m.VIEW_ARCHIVED_LIST
        state.archived_side = SIDE_LEFT
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] modules archived-list skipped: {exc}", flush=True)


def _force_modules_archived_detail_expanded(shell) -> None:
    """Modules ARCHIVED-DETAIL view (LEFT) with the newest fingerprint EXPANDED.

    Drives the archived-detail expanded fingerprint card (modules.py:972 — the
    second ``60 + 24*8`` magic-height row, sibling of :716). Relies on the seeded
    ARCHIVED_LEFT_V1 passport being present in the archive directory.
    """

    try:
        from zd_app.storage.module_passport_models import SIDE_LEFT
        from zd_app.ui.screens import modules as m

        state = m._ensure_state(shell)
        entries = shell.module_passport_service.list_archive_entries(SIDE_LEFT)
        if not entries:
            print("  [seed] modules archived-detail skipped: no archive entries", flush=True)
            return
        state.view = m.VIEW_ARCHIVED_DETAIL
        state.archived_side = SIDE_LEFT
        state.archived_detail_index = 0
        archived_passport, _ = entries[0]
        n = len(archived_passport.fingerprints)
        state.archived_expanded_fingerprint_idx = (n - 1) if n else None
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] modules archived-detail skipped: {exc}", flush=True)


def _force_modules_trends(shell) -> None:
    """Modules MODULE-TRENDS view (LEFT) — the per-metric trend rows (:1390)."""

    try:
        from zd_app.storage.module_passport_models import SIDE_LEFT
        from zd_app.ui.screens import modules as m

        state = m._ensure_state(shell)
        state.view = m.VIEW_MODULE_TRENDS
        state.detail_side = SIDE_LEFT
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] modules trends skipped: {exc}", flush=True)


def _force_readiness_done_red(shell) -> None:
    """Drive Readiness Check to the DONE view with a RED verdict + 4 observations.

    The default QuickCheckService renders the INIT view (IDLE), so the verdict
    card (readiness_check.py:244, fixed 200px) never rendered under the old probe.
    Worst realistic case: a RED verdict carrying the 4-observation cap (rest high
    + range uneven + trigger noisy + cadence inconsistent — the longest-wrapping
    red set), each bullet wrapping at the card's wrap=480 budget.
    """

    try:
        from zd_app.services.health_report import (
            QuickCheckState,
            ReadinessStatus,
            ReadinessVerdict,
        )
        from zd_app.services.health_report import quick_check as qc

        svc = shell.quick_check_service
        # state/verdict are read-only properties; seed the private fields directly
        # rather than driving a real 20-second collection run.
        svc._state = QuickCheckState.COMPLETE
        svc._verdict = ReadinessVerdict(
            status=ReadinessStatus.RED,
            observations=(
                qc.OBS_REST_HIGH,
                qc.OBS_RANGE_RETEST,
                qc.OBS_TRIGGER_NOISY,
                qc.OBS_CADENCE_INCONSISTENT,
            ),
        )
    except Exception as exc:  # pragma: no cover
        print(f"  [seed] readiness done-red skipped: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Per-screen measurement
# ---------------------------------------------------------------------------


def _measure_screen(dpg, shell, screen: str, *, view_setup=None) -> list[dict]:
    if view_setup is not None:
        view_setup(shell)
    shell.switch_screen(screen)
    for _ in range(SETTLE_FRAMES):
        dpg.render_dearpygui_frame()

    kids = dpg.get_item_children("content_region", 1) or []
    if not kids:
        return []
    root = kids[0]
    return _walk_cards(dpg, root)


PREVIEW_ROOT_TAGS = ("safe_import_summary_card", "safe_import_diff_region")


def _open_safe_import_preview(shell, tmp: Path) -> None:
    """Drive the REAL Safe Import scan flow with a rich, multi-category diff.

    Modal-only screen: no switch_screen. Mirrors test_safe_import.py's verified
    flow (open -> set path -> scan -> drain twice) so the preview cards
    (safe_import_summary_card @ 132px, safe_import_diff_region @ 210px) render
    with content from every risk category — the maximally tall case.
    """

    import json

    import dearpygui.dearpygui as dpg

    from zd_app.models import WrapperProfile
    from zd_app.services.settings_service import (
        ButtonMapping,
        ButtonSlot,
        ControllerSnapshot,
        PollingRate,
        SensitivityAnchor,
        StickDeadzones,
        TriggerMode,
        TriggerSettings,
        TriggerVibrationMode,
        VibrationSettings,
    )
    from zd_app.ui.screens import safe_import

    anchors = tuple(
        SensitivityAnchor(x=x, y=y)
        for x, y in ((0, 5), (14, 20), (29, 35), (43, 50), (57, 65), (71, 80), (86, 90), (100, 100))
    )
    # Mirror test_safe_import.py::_snapshot (verified to serialize) and add the
    # extra recognised fields it omits, so the diff spans DEVICE + FEEL + LAYOUT
    # + COSMETIC with many rows. Kept to fields the codec round-trips cleanly.
    snapshot = ControllerSnapshot(
        polling_rate=PollingRate.HZ_8000,  # DEVICE
        vibration=VibrationSettings(15, 15, 15, 15, TriggerVibrationMode.NATIVE),  # COSMETIC
        deadzones=StickDeadzones(5, 5, 95, 95),  # FEEL
        axis_inversion_left=None,
        axis_inversion_right=None,
        sensitivity_left=anchors,  # FEEL
        sensitivity_right=anchors,  # FEEL
        trigger_left=TriggerSettings(0, 100, TriggerMode.SHORT),  # FEEL
        trigger_right=TriggerSettings(5, 95, TriggerMode.LONG),  # FEEL
        button_bindings={
            ButtonSlot.A: ButtonMapping(0, 0, 0),
            ButtonSlot.B: ButtonMapping(1, 0, 0),
            ButtonSlot.X: ButtonMapping(2, 0, 0),
            ButtonSlot.Y: ButtonMapping(3, 0, 0),
        },  # LAYOUT
        lighting_zones={},
    )
    payload = WrapperProfile(name="Maximal Import (probe)", snapshot=snapshot).to_dict()
    path = tmp / "probe_import.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    shell.open_safe_import()
    dpg.set_value(safe_import.PATH_INPUT, str(path))
    shell.safe_import_scan_path()
    # The preview swaps in over two deferred passes (DPG modal law).
    for _ in range(4):
        try:
            shell._drain_deferred_ui_calls()
        except Exception:
            break


def _measure_safe_import_modal(dpg, shell, tmp: Path) -> list[dict]:
    """Open the Safe Import preview modal (it has no nav screen) and walk it."""

    _open_safe_import_preview(shell, tmp)
    for _ in range(SETTLE_FRAMES):
        dpg.render_dearpygui_frame()

    cards: list[dict] = []
    for tag in PREVIEW_ROOT_TAGS:
        if dpg.does_item_exist(tag):
            if _is_child_window(dpg, tag):
                try:
                    cfg = dpg.get_item_configuration(tag)
                    cards.append(
                        {
                            "item": tag,
                            "tag": tag,
                            "path": (),
                            "y_scroll_max": float(dpg.get_y_scroll_max(tag)),
                            "x_scroll_max": float(dpg.get_x_scroll_max(tag)),
                            "cfg_height": cfg.get("height"),
                            "cfg_width": cfg.get("width"),
                            "autosize_y": cfg.get("autosize_y"),
                            "auto_resize_y": cfg.get("auto_resize_y"),
                            "rect": list(dpg.get_item_rect_size(tag)),
                            "sample": _sample_texts(dpg, tag),
                        }
                    )
                except Exception:
                    pass
            cards.extend(_walk_cards(dpg, tag))
    return cards


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _run(screens: list[str], locale: str, width: int, height: int) -> int:
    import dearpygui.dearpygui as dpg
    from unittest.mock import MagicMock

    from zd_app import i18n
    from zd_app.ui.fonts import bind_default_font, register_fonts
    from tests.r2_shell_test_helpers import make_shell

    i18n.set_locale(locale)

    shell = make_shell(settings_service=MagicMock())
    try:
        shell.settings.language = locale
    except Exception:
        pass

    dpg.create_context()
    # Replicate AppShell.run's font path (app_shell.py:1258-1260) so cards are
    # measured at the SHIPPED faces, not Dear PyGui's smaller default.
    register_fonts()
    shell._setup_theme()
    bind_default_font(locale)

    dpg.create_viewport(
        title="card-clip bench", width=width, height=height, min_width=1180, min_height=760
    )
    dpg.setup_dearpygui()
    shell._build_ui()
    dpg.show_viewport()
    for _ in range(SETTLE_FRAMES):
        dpg.render_dearpygui_frame()

    tmpdir = tempfile.TemporaryDirectory()
    _seed_services(shell, Path(tmpdir.name))

    print(
        f"\n=== card-clip matrix  locale={locale}  viewport={width}x{height} ===",
        flush=True,
    )
    any_clip = False

    measurements: list[tuple[str, list[dict]]] = []
    for screen in screens:
        try:
            cards = _measure_screen(dpg, shell, screen)
        except Exception as exc:  # pragma: no cover - diagnostic resilience
            print(f"  {screen:<18} ERROR: {type(exc).__name__}: {exc}", flush=True)
            continue
        measurements.append((screen, cards))

    # Modules compare sub-view (force the COMPARE state).
    if "modules" in screens:
        try:
            cards = _measure_screen(dpg, shell, "modules", view_setup=_force_modules_compare)
            measurements.append(("modules:compare", cards))
        except Exception as exc:  # pragma: no cover
            print(f"  modules:compare ERROR: {type(exc).__name__}: {exc}", flush=True)

    # Restore Points detail sub-view (force DETAIL on a seeded point).
    if "restore_points" in screens:
        try:
            cards = _measure_screen(
                dpg, shell, "restore_points", view_setup=_force_restore_detail
            )
            measurements.append(("restore_points:detail", cards))
        except Exception as exc:  # pragma: no cover
            print(f"  restore_points:detail ERROR: {type(exc).__name__}: {exc}", flush=True)
        try:
            cards = _measure_screen(
                dpg, shell, "restore_points", view_setup=_force_restore_result
            )
            measurements.append(("restore_points:result", cards))
        except Exception as exc:  # pragma: no cover
            print(f"  restore_points:result ERROR: {type(exc).__name__}: {exc}", flush=True)

    # Modules content-heavy sub-views the original probe never reached: an
    # EXPANDED fingerprint (active + archived), the archived list, and the trends
    # view. These exercise the magic-height rows (:716 / :972) and the conditional
    # cards (:637 attention banner, :841 archived row, :1390 trend metric).
    if "modules" in screens:
        for label, setup in (
            ("modules:detail+exp", _force_modules_detail_expanded),
            ("modules:archived", _force_modules_archived_list),
            ("modules:archived+exp", _force_modules_archived_detail_expanded),
            ("modules:trends", _force_modules_trends),
        ):
            try:
                cards = _measure_screen(dpg, shell, "modules", view_setup=setup)
                measurements.append((label, cards))
            except Exception as exc:  # pragma: no cover
                print(f"  {label} ERROR: {type(exc).__name__}: {exc}", flush=True)

    # Readiness Check DONE verdict (RED + 4 observations) — the verdict card :244.
    if "readiness_check" in screens:
        try:
            cards = _measure_screen(
                dpg, shell, "readiness_check", view_setup=_force_readiness_done_red
            )
            measurements.append(("readiness:done", cards))
        except Exception as exc:  # pragma: no cover
            print(f"  readiness:done ERROR: {type(exc).__name__}: {exc}", flush=True)

    for screen, cards in measurements:
        clippers = [c for c in cards if _clip_kind(c) == "real"]
        intentional = [c for c in cards if _clip_kind(c) == "intentional"]
        status = "CLIP" if clippers else "ok"
        if clippers:
            any_clip = True
        extra = f", {len(intentional)} scroll-region" if intentional else ""
        print(
            f"  {screen:<18} {status:<5} ({len(cards)} cards, {len(clippers)} clip{extra})",
            flush=True,
        )
        for c in clippers:
            print(f"      - CLIP  {_format_card(c)}", flush=True)
        for c in intentional:
            print(f"      - (intentional scroll) {_format_card(c)}", flush=True)

    # Safe Import modal measurement (best-effort; isolated so a seeding failure
    # cannot abort the whole run).
    try:
        si_cards = _measure_safe_import_modal(dpg, shell, Path(tmpdir.name))
        clippers = [c for c in si_cards if _clip_kind(c) == "real"]
        intentional = [c for c in si_cards if _clip_kind(c) == "intentional"]
        if si_cards:
            status = "CLIP" if clippers else "ok"
            if clippers:
                any_clip = True
            extra = f", {len(intentional)} scroll-region" if intentional else ""
            print(
                f"  {'safe_import:modal':<18} {status:<5} "
                f"({len(si_cards)} cards, {len(clippers)} clip{extra})",
                flush=True,
            )
            for c in clippers:
                print(f"      - CLIP  {_format_card(c)}", flush=True)
            for c in intentional:
                print(f"      - (intentional scroll) {_format_card(c)}", flush=True)
    except Exception as exc:  # pragma: no cover
        print(f"  safe_import:modal skipped: {exc}", flush=True)

    print(f"{RESULT_PREFIX} locale={locale} size={width}x{height} any_clip={any_clip}", flush=True)
    sys.stdout.flush()

    dpg.destroy_context()
    tmpdir.cleanup()
    return 1 if any_clip else 0


# ---------------------------------------------------------------------------
# Matrix driver (spawns one subprocess per locale x size)
# ---------------------------------------------------------------------------


def _run_matrix() -> int:
    locales = ["en", "zh-CN"]
    sizes = [(1480, 920), (1920, 1040), (1180, 760)]
    rc = 0
    for locale in locales:
        for w, h in sizes:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                "--locale",
                locale,
                "--width",
                str(w),
                "--height",
                str(h),
            ]
            print(f"\n########## {locale} {w}x{h} ##########", flush=True)
            proc = subprocess.run(cmd)
            # rc 1 just means a clip was found; surface but keep going.
            if proc.returncode not in (0, 1):
                rc = proc.returncode
    return rc


def main() -> None:
    argv = sys.argv[1:]
    if "--matrix" in argv:
        raise SystemExit(_run_matrix())

    locale = "en"
    width, height = 1480, 920
    if "--locale" in argv:
        locale = argv[argv.index("--locale") + 1]
    if "--width" in argv:
        width = int(argv[argv.index("--width") + 1])
    if "--height" in argv:
        height = int(argv[argv.index("--height") + 1])
    if "--screen" in argv:
        screens = [argv[argv.index("--screen") + 1]]
    else:
        screens = list(SCREENS)

    raise SystemExit(_run(screens, locale, width, height))


if __name__ == "__main__":
    main()
