"""Self-contained shareable evidence card renderer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zd_app.i18n import t
from zd_app.models import DeviceState
from zd_app.services.compatibility_report import (
    CompatibilityReport,
    build_compatibility_report,
)
from zd_app.services.diagnostic_bundle import (
    CLAIM_BOUNDARY_PARAGRAPH,
    DiagnosticBundlePreviewItem,
    DiagnosticBundlePreviewManifest,
    DiagnosticBundleService,
)
from zd_app.services.diagnostics_service import _redact_instance_id
from zd_app.services.markdown_safety import escape_markdown
from zd_app.services.path_scrub import scrub_paths
from zd_app.services.trust_self_check import (
    TrustSelfCheckResult,
    build_trust_self_check,
)


DEFAULT_HEALTH_REPORT_LIMIT = 3
DEFAULT_WEAR_LEDGER_DAYS = 90
FORBIDDEN_OVERCLAIM_PHRASES = (
    "certified",
    "approved",
    "tournament-safe",
    "vendor-verified",
    "guaranteed",
    "pii-free",
)

_INSTANCE_ID_INLINE_RE = re.compile(
    r"(?:[A-Za-z0-9_]+\\)?VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}"
    r"(?:[&\\][^\s|`\])]+)*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ShareCardSignal:
    label: str
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShareCardBundlePosture:
    label: str
    scope: str
    privacy: str
    included: bool


@dataclass(frozen=True)
class ShareCard:
    generated_at: str
    trust: TrustSelfCheckResult
    compatibility: CompatibilityReport
    signals: tuple[ShareCardSignal, ...]
    bundle_posture: tuple[ShareCardBundlePosture, ...]
    bundle_claim_boundary: str = CLAIM_BOUNDARY_PARAGRAPH

    def to_markdown(self) -> str:
        """Render a portable Markdown copy of the evidence card."""

        lines = [
            f"# {_md(t('share_card.markdown.title'))}",
            "",
            _md(t("share_card.markdown.intro")),
            "",
            f"- {_md(t('share_card.generated_label'))}: {_md(self.generated_at)}",
            f"- {_md(t('compat_report.version_label'))}: {_md(self.compatibility.version)}",
            f"- {_md(t('compat_report.commit_label'))}: {_md(self.compatibility.build_commit)}",
            f"- {_md(t('compat_report.scope_label'))}: {_md(self.compatibility.evidence_scope)}",
            "",
            f"## {_md(t('share_card.section.trust'))}",
            (
                f"| {_md(t('share_card.table.claim'))} | "
                f"{_md(t('share_card.table.evidence'))} | "
                f"{_md(t('share_card.table.boundary'))} |"
            ),
            "| --- | --- | --- |",
        ]
        for row in self.trust.rows:
            lines.append(
                f"| {_md(row.claim)} | {_md(row.evidence)} | {_md(row.boundary)} |"
            )

        lines.extend(
            (
                "",
                f"## {_md(t('share_card.section.device'))}",
                f"- {_md(t('compat_report.product_label'))}: {_md(self.compatibility.product_name)}",
                f"- {_md(t('compat_report.device_model_label'))}: {_md(self.compatibility.device_model_id)}",
                f"- {_md(t('compat_report.variant_label'))}: {_md(self.compatibility.variant)}",
                f"- {_md(t('compat_report.firmware_label'))}: {_md(self.compatibility.firmware)}",
                f"- {_md(t('compat_report.xinput_slot_label'))}: {_md(self.compatibility.active_xinput_slot)}",
                f"- {_md(t('compat_report.live_verify_label'))}: {_md(self.compatibility.live_verify_availability)}",
                "",
                f"### {_md(t('share_card.section.tested_settings'))}",
                (
                    f"| {_md(t('compat_report.table.area'))} | "
                    f"{_md(t('compat_report.table.state'))} | "
                    f"{_md(t('compat_report.table.evidence'))} |"
                ),
                "| --- | --- | --- |",
            )
        )
        for item in self.compatibility.checklist:
            lines.append(
                f"| {_md(item.area)} | {_md(t(f'compat_report.state.{item.state}'))} | {_md(item.evidence)} |"
            )

        lines.extend(
            (
                "",
                f"## {_md(t('share_card.section.signals'))}",
            )
        )
        for signal in self.signals:
            lines.append(f"### {_md(signal.label)}")
            lines.append(f"- {_md(signal.summary)}")
            for detail in signal.details:
                lines.append(f"  - {_md(detail)}")

        included = tuple(item for item in self.bundle_posture if item.included)
        excluded = tuple(item for item in self.bundle_posture if not item.included)
        lines.extend(
            (
                "",
                f"## {_md(t('share_card.section.bundle_posture'))}",
                f"### {_md(t('share_card.bundle.included'))}",
            )
        )
        for item in included:
            lines.extend(_bundle_posture_markdown(item))
        lines.append(f"### {_md(t('share_card.bundle.not_included'))}")
        for item in excluded:
            lines.extend(_bundle_posture_markdown(item))

        lines.extend(
            (
                "",
                f"## {_md(t('share_card.section.claim_boundary'))}",
                f"- {_md(t('share_card.boundary.manual'))}",
                f"- {_md(self.trust.rows[0].boundary if self.trust.rows else t('trust_self_check.boundary.session'))}",
                f"- {_md(self.compatibility.claim_boundary)}",
                f"- {_md(self.bundle_claim_boundary)}",
            )
        )
        return "\n".join(lines).rstrip() + "\n"

    def to_html(self) -> str:
        """Render one offline HTML file with inline CSS and no script."""

        return "\n".join(
            (
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                f"<title>{_html(t('share_card.markdown.title'))}</title>",
                "<style>",
                _INLINE_CSS,
                "</style>",
                "</head>",
                "<body>",
                "<main>",
                _html_header(self),
                _html_trust(self),
                _html_device(self),
                _html_signals(self),
                _html_bundle_posture(self),
                _html_claim_boundary(self),
                "</main>",
                "</body>",
                "</html>",
            )
        )


def build_share_card(
    *,
    device_state: DeviceState | None = None,
    variant: str = "",
    firmware: str = "",
    last_read_duration_ms: float | None = None,
    last_write_duration_ms: float | None = None,
    last_apply_result: object | None = None,
    recent_events: Iterable[str] = (),
    diagnostic_bundle_path: str | Path | None = None,
    diagnostic_bundle_service: DiagnosticBundleService | None = None,
    trust_self_check: TrustSelfCheckResult | None = None,
    compatibility_report: CompatibilityReport | None = None,
    now: datetime | None = None,
    health_report_limit: int = DEFAULT_HEALTH_REPORT_LIMIT,
    wear_ledger_days: int = DEFAULT_WEAR_LEDGER_DAYS,
) -> ShareCard:
    """Assemble the evidence card without networking or device I/O."""

    moment = now or datetime.now(timezone.utc)
    generated_at = moment.isoformat(timespec="seconds")
    trust = trust_self_check or build_trust_self_check(now=moment)
    compatibility = compatibility_report or build_compatibility_report(
        device_state=device_state,
        variant=variant,
        firmware=firmware,
        last_read_duration_ms=last_read_duration_ms,
        last_write_duration_ms=last_write_duration_ms,
        last_apply_result=last_apply_result,
        recent_events=recent_events,
        diagnostic_bundle_path=diagnostic_bundle_path,
        now=moment,
    )
    manifest = _preview_manifest(
        diagnostic_bundle_service,
        health_report_limit=health_report_limit,
        wear_ledger_days=wear_ledger_days,
    )
    signals = _signals(
        diagnostic_bundle_service,
        manifest,
        health_report_limit=health_report_limit,
        wear_ledger_days=wear_ledger_days,
    )
    return ShareCard(
        generated_at=generated_at,
        trust=trust,
        compatibility=compatibility,
        signals=signals,
        bundle_posture=_bundle_posture(manifest),
    )


def _preview_manifest(
    bundle: DiagnosticBundleService | None,
    *,
    health_report_limit: int,
    wear_ledger_days: int,
) -> DiagnosticBundlePreviewManifest | None:
    if not isinstance(bundle, DiagnosticBundleService):
        return None
    try:
        return bundle.preview_bundle_manifest(
            include_archived=True,
            health_report_limit=health_report_limit,
            wear_ledger_days=wear_ledger_days,
        )
    except Exception:  # noqa: BLE001 - share card should degrade, not crash
        return None


def _signals(
    bundle: DiagnosticBundleService | None,
    manifest: DiagnosticBundlePreviewManifest | None,
    *,
    health_report_limit: int,
    wear_ledger_days: int,
) -> tuple[ShareCardSignal, ...]:
    return (
        *_health_signals(bundle, limit=health_report_limit),
        _readiness_signal(bundle, wear_ledger_days=wear_ledger_days),
        _module_signal(manifest, bundle, wear_ledger_days=wear_ledger_days),
        _wear_signal(bundle, wear_ledger_days=wear_ledger_days),
    )


def _health_signals(
    bundle: DiagnosticBundleService | None,
    *,
    limit: int,
) -> tuple[ShareCardSignal, ...]:
    if not isinstance(bundle, DiagnosticBundleService):
        return (
            ShareCardSignal(
                label=t("share_card.signal.health"),
                summary=t("share_card.signal.health.unavailable"),
            ),
        )
    try:
        files = bundle._list_recent_health_reports(limit=limit)
    except Exception:  # noqa: BLE001 - defensive, same best-effort posture as bundle
        files = []
    if not files:
        return (
            ShareCardSignal(
                label=t("share_card.signal.health"),
                summary=t("share_card.signal.health.none"),
            ),
        )

    signals: list[ShareCardSignal] = []
    for path in files:
        try:
            summary = bundle._summarize_health_report(path)
        except Exception:  # noqa: BLE001
            summary = {"filename": path.name, "lines": [t("share_card.signal.health.unreadable")]}
        filename = _clean(summary.get("filename") or path.name)
        lines = tuple(_clean(line) for line in summary.get("lines", ()) if _clean(line))
        signals.append(
            ShareCardSignal(
                label=t("share_card.signal.health"),
                summary=t("share_card.signal.health.report", filename=filename),
                details=lines,
            )
        )
    return tuple(signals)


def _readiness_signal(
    bundle: DiagnosticBundleService | None,
    *,
    wear_ledger_days: int,
) -> ShareCardSignal:
    summary = _wear_summary(bundle, wear_ledger_days=wear_ledger_days)
    verdicts = _string_int_mapping(summary.get("readiness_check_verdicts", {}))
    if not verdicts:
        return ShareCardSignal(
            label=t("share_card.signal.readiness"),
            summary=t("share_card.signal.readiness.none", days=wear_ledger_days),
        )
    return ShareCardSignal(
        label=t("share_card.signal.readiness"),
        summary=t("share_card.signal.readiness.summary", days=wear_ledger_days),
        details=_count_lines(verdicts),
    )


def _module_signal(
    manifest: DiagnosticBundlePreviewManifest | None,
    bundle: DiagnosticBundleService | None,
    *,
    wear_ledger_days: int,
) -> ShareCardSignal:
    module_item = _manifest_item(manifest, "module_passports")
    metadata = module_item.metadata if module_item is not None else {}
    active_count = _int(metadata.get("active_count"), 0)
    archived_count = _int(metadata.get("archived_count"), 0)
    summary = _wear_summary(bundle, wear_ledger_days=wear_ledger_days)
    verdicts = _string_int_mapping(summary.get("module_characterized_verdicts", {}))
    details = [
        t("share_card.signal.modules.active", count=active_count),
        t("share_card.signal.modules.archived", count=archived_count),
    ]
    details.extend(_count_lines(verdicts))
    return ShareCardSignal(
        label=t("share_card.signal.modules"),
        summary=t("share_card.signal.modules.summary", days=wear_ledger_days),
        details=tuple(details),
    )


def _wear_signal(
    bundle: DiagnosticBundleService | None,
    *,
    wear_ledger_days: int,
) -> ShareCardSignal:
    summary = _wear_summary(bundle, wear_ledger_days=wear_ledger_days)
    counts = _string_int_mapping(summary.get("counts", {}))
    if not counts:
        return ShareCardSignal(
            label=t("share_card.signal.wear"),
            summary=t("share_card.signal.wear.none", days=wear_ledger_days),
        )
    keys = (
        "session_start",
        "profile_apply",
        "rp_capture",
        "rp_restore",
        "health_report",
        "readiness_check",
        "module_characterized",
        "service_note",
        "diagnostic_bundle_generated",
    )
    details = tuple(
        t(f"share_card.signal.wear.count.{key}", count=counts.get(key, 0))
        for key in keys
    )
    return ShareCardSignal(
        label=t("share_card.signal.wear"),
        summary=t("share_card.signal.wear.summary", days=wear_ledger_days),
        details=details,
    )


def _wear_summary(
    bundle: DiagnosticBundleService | None,
    *,
    wear_ledger_days: int,
) -> Mapping[str, Any]:
    if not isinstance(bundle, DiagnosticBundleService):
        return {}
    try:
        return bundle._wear_ledger_summary(wear_ledger_days=wear_ledger_days)
    except Exception:  # noqa: BLE001
        return {}


def _bundle_posture(
    manifest: DiagnosticBundlePreviewManifest | None,
) -> tuple[ShareCardBundlePosture, ...]:
    if manifest is None:
        return (
            ShareCardBundlePosture(
                label=t("share_card.bundle.unavailable.label"),
                scope=t("share_card.bundle.unavailable.scope"),
                privacy=t("share_card.bundle.unavailable.privacy"),
                included=True,
            ),
            ShareCardBundlePosture(
                label=t("diagnostics.bundle_preview.raw_event_log.label"),
                scope=t("diagnostics.bundle_preview.raw_event_log.scope"),
                privacy=t("diagnostics.bundle_preview.raw_event_log.privacy"),
                included=False,
            ),
            ShareCardBundlePosture(
                label=t("diagnostics.bundle_preview.raw_app_logs.label"),
                scope=t("diagnostics.bundle_preview.raw_app_logs.scope"),
                privacy=t("diagnostics.bundle_preview.raw_app_logs.privacy"),
                included=False,
            ),
        )

    return tuple(
        _posture_from_item(item, included=True) for item in manifest.items
    ) + tuple(
        _posture_from_item(item, included=False) for item in manifest.excluded_items
    )


def _posture_from_item(
    item: DiagnosticBundlePreviewItem,
    *,
    included: bool,
) -> ShareCardBundlePosture:
    return ShareCardBundlePosture(
        label=t(f"diagnostics.bundle_preview.{item.key}.label"),
        scope=_bundle_scope(item),
        privacy=t(f"diagnostics.bundle_preview.{item.key}.privacy"),
        included=included,
    )


def _bundle_scope(item: DiagnosticBundlePreviewItem) -> str:
    metadata = dict(item.metadata)
    if item.key == "report":
        return t(
            "diagnostics.bundle_preview.report.scope",
            member_count=item.member_count,
        )
    if item.key == "module_passports":
        return t(
            "diagnostics.bundle_preview.module_passports.scope",
            member_count=item.member_count,
            active_count=_int(metadata.get("active_count"), 0),
            archived_count=_int(metadata.get("archived_count"), 0),
        )
    if item.key == "health_reports":
        return t(
            "diagnostics.bundle_preview.health_reports.scope",
            report_count=item.count,
            member_count=item.member_count,
            markdown_count=_int(metadata.get("markdown_count"), 0),
            json_count=_int(metadata.get("json_count"), 0),
        )
    if item.key == "wear_ledger":
        return t(
            "diagnostics.bundle_preview.wear_ledger.scope",
            window_days=_int(metadata.get("window_days"), DEFAULT_WEAR_LEDGER_DAYS),
            event_count=_int(metadata.get("event_count"), 0),
            service_note_count=_int(metadata.get("service_note_count"), 0),
        )
    return t(f"diagnostics.bundle_preview.{item.key}.scope")


def _html_header(card: ShareCard) -> str:
    rows = (
        (t("share_card.generated_label"), card.generated_at),
        (t("compat_report.version_label"), card.compatibility.version),
        (t("compat_report.commit_label"), card.compatibility.build_commit),
        (t("compat_report.scope_label"), card.compatibility.evidence_scope),
    )
    return (
        "<header>"
        f"<p class=\"eyebrow\">{_html(t('share_card.eyebrow'))}</p>"
        f"<h1>{_html(t('share_card.markdown.title'))}</h1>"
        f"<p>{_html(t('share_card.markdown.intro'))}</p>"
        "<dl>"
        + "".join(
            f"<div><dt>{_html(label)}</dt><dd>{_html(value)}</dd></div>"
            for label, value in rows
        )
        + "</dl>"
        "</header>"
    )


def _html_trust(card: ShareCard) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_html(row.claim)}</td>"
        f"<td>{_html(row.evidence)}</td>"
        f"<td>{_html(row.boundary)}</td>"
        "</tr>"
        for row in card.trust.rows
    )
    return (
        f"<section><h2>{_html(t('share_card.section.trust'))}</h2>"
        "<div class=\"table-wrap\"><table><thead><tr>"
        f"<th>{_html(t('share_card.table.claim'))}</th>"
        f"<th>{_html(t('share_card.table.evidence'))}</th>"
        f"<th>{_html(t('share_card.table.boundary'))}</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _html_device(card: ShareCard) -> str:
    compat = card.compatibility
    facts = (
        (t("compat_report.product_label"), compat.product_name),
        (t("compat_report.device_model_label"), compat.device_model_id),
        (t("compat_report.variant_label"), compat.variant),
        (t("compat_report.firmware_label"), compat.firmware),
        (t("compat_report.xinput_slot_label"), compat.active_xinput_slot),
        (t("compat_report.live_verify_label"), compat.live_verify_availability),
    )
    checklist = "".join(
        "<tr>"
        f"<td>{_html(item.area)}</td>"
        f"<td>{_html(t(f'compat_report.state.{item.state}'))}</td>"
        f"<td>{_html(item.evidence)}</td>"
        "</tr>"
        for item in compat.checklist
    )
    return (
        f"<section><h2>{_html(t('share_card.section.device'))}</h2>"
        "<div class=\"facts\">"
        + "".join(
            f"<div><span>{_html(label)}</span><strong>{_html(value)}</strong></div>"
            for label, value in facts
        )
        + "</div>"
        f"<h3>{_html(t('share_card.section.tested_settings'))}</h3>"
        "<div class=\"table-wrap\"><table><thead><tr>"
        f"<th>{_html(t('compat_report.table.area'))}</th>"
        f"<th>{_html(t('compat_report.table.state'))}</th>"
        f"<th>{_html(t('compat_report.table.evidence'))}</th>"
        f"</tr></thead><tbody>{checklist}</tbody></table></div></section>"
    )


def _html_signals(card: ShareCard) -> str:
    cards = []
    for signal in card.signals:
        details = "".join(f"<li>{_html(detail)}</li>" for detail in signal.details)
        cards.append(
            "<article>"
            f"<h3>{_html(signal.label)}</h3>"
            f"<p>{_html(signal.summary)}</p>"
            + (f"<ul>{details}</ul>" if details else "")
            + "</article>"
        )
    return (
        f"<section><h2>{_html(t('share_card.section.signals'))}</h2>"
        f"<div class=\"signals\">{''.join(cards)}</div></section>"
    )


def _html_bundle_posture(card: ShareCard) -> str:
    included = tuple(item for item in card.bundle_posture if item.included)
    excluded = tuple(item for item in card.bundle_posture if not item.included)
    return (
        f"<section><h2>{_html(t('share_card.section.bundle_posture'))}</h2>"
        "<div class=\"bundle-grid\">"
        f"<article><h3>{_html(t('share_card.bundle.included'))}</h3>"
        + _html_bundle_list(included)
        + "</article>"
        f"<article><h3>{_html(t('share_card.bundle.not_included'))}</h3>"
        + _html_bundle_list(excluded)
        + "</article>"
        "</div></section>"
    )


def _html_bundle_list(items: Sequence[ShareCardBundlePosture]) -> str:
    return "<ul>" + "".join(
        "<li>"
        f"<strong>{_html(item.label)}</strong>"
        f"<span>{_html(item.scope)}</span>"
        f"<em>{_html(item.privacy)}</em>"
        "</li>"
        for item in items
    ) + "</ul>"


def _html_claim_boundary(card: ShareCard) -> str:
    boundaries = (
        t("share_card.boundary.manual"),
        card.trust.rows[0].boundary if card.trust.rows else t("trust_self_check.boundary.session"),
        card.compatibility.claim_boundary,
        card.bundle_claim_boundary,
    )
    return (
        f"<section><h2>{_html(t('share_card.section.claim_boundary'))}</h2>"
        "<ul class=\"boundary\">"
        + "".join(f"<li>{_html(item)}</li>" for item in boundaries)
        + "</ul></section>"
    )


def _bundle_posture_markdown(item: ShareCardBundlePosture) -> tuple[str, ...]:
    return (
        f"- {_md(item.label)}",
        f"  - {_md(item.scope)}",
        f"  - {_md(t('diagnostics.bundle_preview.privacy_line', posture=item.privacy))}",
    )


def _count_lines(counts: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(f"{_clean(key)}: {count}" for key, count in sorted(counts.items()))


def _manifest_item(
    manifest: DiagnosticBundlePreviewManifest | None,
    key: str,
) -> DiagnosticBundlePreviewItem | None:
    if manifest is None:
        return None
    return next((item for item in manifest.items if item.key == key), None)


def _string_int_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        result[_clean(key)] = _int(count, 0)
    return result


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _redact_inline_instance_ids(text: str) -> str:
    return _INSTANCE_ID_INLINE_RE.sub(lambda match: _redact_instance_id(match.group(0)), text)


def _clean(value: object) -> str:
    text = "" if value is None else str(value)
    text = _redact_inline_instance_ids(text)
    text = scrub_paths(text)
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines()).strip()


def _md(value: object) -> str:
    return escape_markdown(_clean(value))


def _html(value: object) -> str:
    return html_escape(_clean(value), quote=True)


_INLINE_CSS = """
:root {
  color-scheme: light;
  --ink: #1f2933;
  --muted: #5d6878;
  --line: #d8dee8;
  --panel: #f7f9fb;
  --accent: #0f766e;
  --accent-soft: #e7f4f2;
  --warn-soft: #fff5df;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  background: #eef2f6;
  color: var(--ink);
  font-family: "Segoe UI", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.48;
  letter-spacing: 0;
}
main {
  width: min(1120px, calc(100vw - 32px));
  margin: 24px auto;
  background: #ffffff;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 18px 40px rgba(31, 41, 51, 0.10);
}
header {
  padding: 28px 32px;
  background: linear-gradient(180deg, #ffffff, var(--accent-soft));
  border-bottom: 1px solid var(--line);
}
.eyebrow {
  margin: 0 0 8px;
  color: var(--accent);
  font-weight: 700;
  text-transform: uppercase;
}
h1, h2, h3, p {
  margin-top: 0;
}
h1 {
  font-size: 34px;
  line-height: 1.12;
  margin-bottom: 10px;
}
h2 {
  font-size: 22px;
  margin-bottom: 14px;
}
h3 {
  font-size: 16px;
  margin: 12px 0 8px;
}
section {
  padding: 24px 32px;
  border-bottom: 1px solid var(--line);
}
dl, .facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px;
}
dl div, .facts div, .signals article, .bundle-grid article {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
}
dt, .facts span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
dd, .facts strong {
  display: block;
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
.table-wrap {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
th, td {
  border: 1px solid var(--line);
  padding: 10px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th {
  background: var(--panel);
  color: var(--muted);
}
.signals, .bundle-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
ul {
  padding-left: 20px;
  margin: 8px 0 0;
}
li {
  margin: 7px 0;
}
.bundle-grid li strong,
.bundle-grid li span,
.bundle-grid li em {
  display: block;
}
.bundle-grid li em {
  color: var(--muted);
  font-style: normal;
}
.boundary {
  background: var(--warn-soft);
  border: 1px solid #ead49a;
  border-radius: 8px;
  padding: 14px 18px 14px 34px;
}
@media (max-width: 700px) {
  main {
    width: 100vw;
    margin: 0;
    border-left: 0;
    border-right: 0;
    border-radius: 0;
  }
  header, section {
    padding: 20px;
  }
  h1 {
    font-size: 27px;
  }
  table {
    min-width: 680px;
  }
}
""".strip()


__all__ = [
    "DEFAULT_HEALTH_REPORT_LIMIT",
    "DEFAULT_WEAR_LEDGER_DAYS",
    "FORBIDDEN_OVERCLAIM_PHRASES",
    "ShareCard",
    "ShareCardBundlePosture",
    "ShareCardSignal",
    "build_share_card",
]
