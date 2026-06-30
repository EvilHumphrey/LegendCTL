"""Share-safe compatibility report evidence for Diagnostics."""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Sequence

from zd_app import version as app_version
from zd_app.i18n import t
from zd_app.models import DeviceState
from zd_app.services._log_entry import LogEntry, render_log_entry
from zd_app.services.diagnostics_service import _redact_instance_id
from zd_app.services.markdown_safety import escape_markdown
from zd_app.services.path_scrub import scrub_paths


CompatibilityState = Literal[
    "read_ok",
    "write_ok",
    "sent_not_verified",
    "failed",
    "not_tried",
]

REPORT_STATES: tuple[CompatibilityState, ...] = (
    "read_ok",
    "write_ok",
    "sent_not_verified",
    "failed",
    "not_tried",
)

ISSUE_TEMPLATE_FIELD_IDS: tuple[str, ...] = (
    "app-version",
    "windows-version",
    "device",
    "overall",
    "per-setting",
    "live-verify",
    "log",
    "notes",
)

_INSTANCE_ID_INLINE_RE = re.compile(
    r"(?:[A-Za-z0-9_]+\\)?VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}"
    r"(?:[&\\][^\s|`\])]+)*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompatibilityChecklistItem:
    area: str
    state: CompatibilityState
    evidence: str


@dataclass(frozen=True)
class CompatibilityReport:
    generated_at: str
    version: str
    build_commit: str
    windows_version: str
    product_name: str
    device_model_id: str
    variant: str
    firmware: str
    active_xinput_slot: str
    live_verify_availability: str
    checklist: tuple[CompatibilityChecklistItem, ...]
    diagnostic_bundle_reference: str
    recent_events: tuple[str, ...]
    overall_result: str
    claim_boundary: str

    def to_markdown(self) -> str:
        """Render the share-safe compatibility packet."""

        lines = [
            f"# {_md(t('compat_report.markdown.title'))}",
            "",
            _md(t("compat_report.markdown.intro")),
            "",
            f"- {_md(t('compat_report.generated_label'))}: {_md(self.generated_at)}",
            f"- {_md(t('compat_report.version_label'))}: {_md(self.version)}",
            f"- {_md(t('compat_report.commit_label'))}: {_md(self.build_commit)}",
            f"- {_md(t('compat_report.windows_label'))}: {_md(self.windows_version)}",
            f"- {_md(t('compat_report.claim_boundary_label'))}: {_md(self.claim_boundary)}",
            f"- {_md(t('compat_report.scope_label'))}: {_md(self.evidence_scope)}",
            "",
            f"## {_md(t('compat_report.section.controller'))}",
            f"- {_md(t('compat_report.product_label'))}: {_md(self.product_name)}",
            f"- {_md(t('compat_report.device_model_label'))}: {_md(self.device_model_id)}",
            f"- {_md(t('compat_report.variant_label'))}: {_md(self.variant)}",
            f"- {_md(t('compat_report.firmware_label'))}: {_md(self.firmware)}",
            f"- {_md(t('compat_report.xinput_slot_label'))}: {_md(self.active_xinput_slot)}",
            f"- {_md(t('compat_report.live_verify_label'))}: {_md(self.live_verify_availability)}",
            "",
            f"## {_md(t('compat_report.section.checklist'))}",
            (
                f"| {_md(t('compat_report.table.area'))} | "
                f"{_md(t('compat_report.table.state'))} | "
                f"{_md(t('compat_report.table.evidence'))} |"
            ),
            "| --- | --- | --- |",
        ]
        for item in self.checklist:
            lines.append(
                f"| {_md(item.area)} | {_md(_state_label(item.state))} | {_md(item.evidence)} |"
            )
        lines.extend(
            (
                "",
                f"## {_md(t('compat_report.section.diagnostic_evidence'))}",
                f"- {_md(t('compat_report.bundle_label'))}: {_md(self.diagnostic_bundle_reference)}",
            )
        )
        if self.recent_events:
            lines.append(f"- {_md(t('compat_report.recent_events_label'))}:")
            for event in self.recent_events:
                lines.append(f"  - {_md(event)}")
        else:
            lines.append(f"- {_md(t('compat_report.recent_events_label'))}: {_md(t('compat_report.recent_events.none'))}")
        return "\n".join(lines).rstrip() + "\n"

    def to_issue_body(self) -> str:
        """Render copy-paste text aligned to .github/ISSUE_TEMPLATE fields."""

        lines = [
            _issue_section(
                "app_version",
                f"{self.version} ({t('compat_report.commit_label')}: {self.build_commit})",
            ),
            _issue_section("windows_version", self.windows_version),
            _issue_section(
                "device",
                (
                    f"{self.product_name}; {self.device_model_id}; "
                    f"{t('compat_report.variant_label')}: {self.variant}; "
                    f"{t('compat_report.firmware_label')}: {self.firmware}"
                ),
            ),
            _issue_section("overall", self.overall_result),
            _issue_section("per_setting", self._checklist_issue_text()),
            _issue_section("live_verify", self.live_verify_availability),
            _issue_section(
                "log",
                self._diagnostic_evidence_issue_text(),
            ),
            _issue_section(
                "notes",
                (
                    f"{self.claim_boundary}\n\n"
                    f"{t('compat_report.scope_label')}: {self.evidence_scope}"
                ),
            ),
        ]
        return "\n\n".join(lines).rstrip() + "\n"

    @property
    def evidence_scope(self) -> str:
        states = {item.state for item in self.checklist}
        if "failed" in states:
            return t("compat_report.scope.failed_or_mixed")
        if "write_ok" in states or "sent_not_verified" in states:
            return t("compat_report.scope.read_write")
        if "read_ok" in states:
            return t("compat_report.scope.limited")
        return t("compat_report.scope.not_observed")

    def _checklist_issue_text(self) -> str:
        return "\n".join(
            f"- {item.area}: {_state_label(item.state)} - {item.evidence}"
            for item in self.checklist
        )

    def _diagnostic_evidence_issue_text(self) -> str:
        lines = [f"{t('compat_report.bundle_label')}: {self.diagnostic_bundle_reference}"]
        if self.recent_events:
            lines.append(t("compat_report.recent_events_label") + ":")
            lines.extend(f"- {event}" for event in self.recent_events)
        else:
            lines.append(
                f"{t('compat_report.recent_events_label')}: "
                f"{t('compat_report.recent_events.none')}"
            )
        return "\n".join(lines)


def build_compatibility_report(
    *,
    device_state: DeviceState | None = None,
    variant: str = "",
    firmware: str = "",
    last_read_duration_ms: float | None = None,
    last_write_duration_ms: float | None = None,
    last_apply_result: object | None = None,
    recent_events: Iterable[str] = (),
    diagnostic_bundle_path: str | Path | None = None,
    windows_version: str | None = None,
    now: datetime | None = None,
    checklist: Sequence[CompatibilityChecklistItem] | None = None,
) -> CompatibilityReport:
    """Build a conservative local compatibility report without device or network I/O."""

    state = device_state or DeviceState()
    generated_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    model_id = _clean(_redact_instance_id(getattr(state, "stable_identifier", "")), t("common.unknown"))
    xinput_slot = _xinput_slot_label(getattr(state, "xinput_slot", None))
    live_verify = _live_verify_availability(state)
    items = tuple(checklist) if checklist is not None else _default_checklist(
        state,
        last_read_duration_ms=last_read_duration_ms,
        last_write_duration_ms=last_write_duration_ms,
        last_apply_result=last_apply_result,
    )
    return CompatibilityReport(
        generated_at=generated_at,
        version=_clean(getattr(app_version, "__version__", ""), t("common.unknown")),
        build_commit=_clean(
            getattr(app_version, "__build_commit__", ""),
            t("compat_report.value.not_embedded"),
        ),
        windows_version=_clean(windows_version or _windows_version(), t("common.unknown")),
        product_name=_clean(getattr(state, "product_name", ""), t("common.unknown")),
        device_model_id=model_id,
        variant=_clean(variant, t("compat_report.value.unknown")),
        firmware=_clean(firmware, t("compat_report.value.unknown")),
        active_xinput_slot=xinput_slot,
        live_verify_availability=live_verify,
        checklist=items,
        diagnostic_bundle_reference=_bundle_reference(diagnostic_bundle_path),
        recent_events=_recent_event_lines(recent_events),
        overall_result=_overall_result(state, items),
        claim_boundary=t("compat_report.claim_boundary"),
    )


def _default_checklist(
    state: DeviceState,
    *,
    last_read_duration_ms: float | None,
    last_write_duration_ms: float | None,
    last_apply_result: object | None,
) -> tuple[CompatibilityChecklistItem, ...]:
    read_state, read_evidence = _read_check(state, last_read_duration_ms)
    write_state, write_evidence = _write_check(
        state,
        last_write_duration_ms=last_write_duration_ms,
        last_apply_result=last_apply_result,
    )
    write_only_state, write_only_evidence = _write_only_check(last_apply_result)
    live_state, live_evidence = _live_verify_check(state)
    return (
        CompatibilityChecklistItem(
            area=t("compat_report.check.read_settings"),
            state=read_state,
            evidence=read_evidence,
        ),
        CompatibilityChecklistItem(
            area=t("compat_report.check.write_settings"),
            state=write_state,
            evidence=write_evidence,
        ),
        CompatibilityChecklistItem(
            area=t("compat_report.check.write_only_fields"),
            state=write_only_state,
            evidence=write_only_evidence,
        ),
        CompatibilityChecklistItem(
            area=t("compat_report.check.live_verify"),
            state=live_state,
            evidence=live_evidence,
        ),
    )


def _read_check(
    state: DeviceState,
    last_read_duration_ms: float | None,
) -> tuple[CompatibilityState, str]:
    last_read = getattr(state, "last_read_time", None)
    if last_read:
        if last_read_duration_ms is None:
            return "read_ok", t("compat_report.evidence.read_ok", detail=last_read)
        return "read_ok", t(
            "compat_report.evidence.read_ok_with_duration",
            detail=last_read,
            duration=f"{last_read_duration_ms:.2f}ms",
        )
    if getattr(state, "connection_state", "") == "device_error":
        return "failed", t("compat_report.evidence.read_failed")
    return "not_tried", t("compat_report.evidence.read_not_tried")


def _write_check(
    state: DeviceState,
    *,
    last_write_duration_ms: float | None,
    last_apply_result: object | None,
) -> tuple[CompatibilityState, str]:
    apply_time = getattr(state, "last_apply_time", None)
    data_freshness = getattr(state, "data_freshness", "")
    apply_summary = _apply_result_text(last_apply_result)
    if data_freshness == "write_failed":
        return "failed", _clean(
            apply_summary or t("compat_report.evidence.write_failed"),
            t("compat_report.evidence.write_failed"),
        )
    if apply_time or last_write_duration_ms is not None:
        if data_freshness == "write_success":
            if last_write_duration_ms is None:
                return "write_ok", t("compat_report.evidence.write_ok", detail=apply_time or t("common.unknown"))
            return "write_ok", t(
                "compat_report.evidence.write_ok_with_duration",
                detail=apply_time or t("common.unknown"),
                duration=f"{last_write_duration_ms:.2f}ms",
            )
        return "sent_not_verified", t("compat_report.evidence.write_sent_unverified")
    return "not_tried", t("compat_report.evidence.write_not_tried")


def _write_only_check(last_apply_result: object | None) -> tuple[CompatibilityState, str]:
    text = _apply_result_text(last_apply_result)
    lowered = text.lower()
    if any(token in lowered for token in ("write-only", "sent", "back-paddle", "paddle")):
        if "fail" in lowered:
            return "failed", _clean(text, t("compat_report.evidence.write_only_failed"))
        return "sent_not_verified", _clean(
            text,
            t("compat_report.evidence.write_only_sent"),
        )
    return "not_tried", t("compat_report.evidence.write_only_not_tried")


def _live_verify_check(state: DeviceState) -> tuple[CompatibilityState, str]:
    if getattr(state, "xinput_slot", None) is not None:
        return "read_ok", t(
            "compat_report.evidence.live_verify_slot",
            slot=getattr(state, "xinput_slot"),
        )
    if getattr(state, "connection_state", "") == "no_device":
        return "not_tried", t("compat_report.evidence.live_verify_no_device")
    if getattr(state, "live_verify_supported", False):
        return "not_tried", t("compat_report.evidence.live_verify_available")
    return "failed", t("compat_report.evidence.live_verify_unavailable")


def _live_verify_availability(state: DeviceState) -> str:
    if getattr(state, "xinput_slot", None) is not None:
        return t("compat_report.live_verify.available_slot", slot=getattr(state, "xinput_slot"))
    if getattr(state, "connection_state", "") == "no_device":
        return t("compat_report.live_verify.not_observed")
    if getattr(state, "live_verify_supported", False):
        return t("compat_report.live_verify.available_no_slot")
    return t("compat_report.live_verify.unavailable")


def _xinput_slot_label(slot: object | None) -> str:
    if slot is None:
        return t("compat_report.value.not_observed")
    return t("compat_report.xinput_slot.value", slot=slot)


def _overall_result(
    state: DeviceState,
    items: tuple[CompatibilityChecklistItem, ...],
) -> str:
    states = {item.state for item in items}
    if getattr(state, "connection_state", "") == "no_device" and "read_ok" not in states:
        return t("compat_report.overall.not_detected")
    if "failed" in states:
        return t("compat_report.overall.mixed")
    if "write_ok" in states:
        return t("compat_report.overall.worked")
    if "read_ok" in states:
        return t("compat_report.overall.live_or_read_only")
    return t("compat_report.overall.other_limited")


def _windows_version() -> str:
    try:
        return platform.platform(terse=True)
    except Exception:  # noqa: BLE001 - platform should never break report generation
        return ""


def _bundle_reference(path: str | Path | None) -> str:
    if path is None or str(path).strip() == "":
        return t("compat_report.bundle.none")
    return _clean(path, t("compat_report.bundle.none"))


def _recent_event_lines(events: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for event in events:
        line = _clean(event, "")
        if line:
            cleaned.append(line)
        if len(cleaned) >= 8:
            break
    return tuple(cleaned)


def _apply_result_text(value: object | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, LogEntry):
        return render_log_entry(value)
    if value is None:
        return ""
    return str(value)


def _clean(value: object, default: str) -> str:
    text = "" if value is None else str(value)
    text = _redact_inline_instance_ids(text)
    text = scrub_paths(text)
    text = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
    return text.strip() or default


def _redact_inline_instance_ids(text: str) -> str:
    return _INSTANCE_ID_INLINE_RE.sub(lambda match: _redact_instance_id(match.group(0)), text)


def _state_label(state: CompatibilityState) -> str:
    return t(f"compat_report.state.{state}")


def _issue_section(label_key: str, body: str) -> str:
    return f"### {t(f'compat_report.issue.{label_key}')}\n{_md_block(body)}"


def _md(value: object) -> str:
    return escape_markdown(value)


def _md_block(value: object) -> str:
    text = "" if value is None else str(value)
    return "\n".join(_md(line) for line in text.splitlines())


__all__ = [
    "ISSUE_TEMPLATE_FIELD_IDS",
    "REPORT_STATES",
    "CompatibilityChecklistItem",
    "CompatibilityReport",
    "CompatibilityState",
    "build_compatibility_report",
]
