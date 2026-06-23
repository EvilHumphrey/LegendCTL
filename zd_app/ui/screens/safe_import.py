"""Safe Import preview UI.

A guarded import flow for external profile files: file select -> safety scan
summary -> category checklist -> human-readable diff -> apply destination ->
result. Nothing is written to disk or the controller before the user picks an
action on the preview. The shell owns the controller logic (scan/apply); this
module owns the modal rendering and reads shell state.
"""

from __future__ import annotations

import json

import dearpygui.dearpygui as dpg

from zd_app.i18n import get_locale, t
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.ui import safe_import_badges as badges
from zd_app.ui.fonts import font_for
from zd_app.ui.safe_import_model import (
    DEFAULT_CHECKED_CATEGORIES,
    ImportResult,
    RiskCategory,
    SELECTABLE_CATEGORIES,
    category_label_key,
    summarize_field,
    summarize_value,
)
from zd_app.ui.typography import section_title

FILE_MODAL = "safe_import_file_modal"
PREVIEW_MODAL = "safe_import_preview_modal"
CONFIRM_MODAL = "safe_import_confirm_modal"
RESULT_MODAL = "safe_import_result_modal"
FILE_DIALOG = "safe_import_file_dialog"

PATH_INPUT = "safe_import_path_input"
FILE_ERROR = "safe_import_file_error"
NAME_INPUT = "safe_import_name_input"

# Diff order: device last + visually separate, automation/blocked greyed.
_DIFF_CATEGORY_ORDER = (
    RiskCategory.FEEL,
    RiskCategory.LAYOUT,
    RiskCategory.COSMETIC,
    RiskCategory.DEVICE,
)


def _delete_if_exists(tag: str) -> None:
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)


def close_modals() -> None:
    """Tear down every Safe Import modal (used on hard errors / cancel)."""

    for tag in (FILE_MODAL, PREVIEW_MODAL, CONFIRM_MODAL, RESULT_MODAL):
        _delete_if_exists(tag)


# --- Step 1: file select ----------------------------------------------------


def open_file_select(shell) -> None:
    _delete_if_exists(FILE_MODAL)
    # autosize, not a fixed height: 210 clipped the Browse/Scan/Cancel row
    # behind an internal scrollbar at default window size (seen in
    # local testing), and the hidden error text grows the content when it
    # shows. width stays as the pre-first-frame size; autosize then fits
    # both dimensions to content.
    with dpg.window(
        tag=FILE_MODAL,
        label=t("safe_import.file_select.title"),
        modal=True,
        no_resize=True,
        width=560,
        autosize=True,
    ):
        dpg.add_text(t("safe_import.header"), wrap=520)
        dpg.add_text(t("safe_import.trust_statement"), color=shell.COLORS["muted"], wrap=520)
        dpg.add_spacer(height=8)
        dpg.add_text(t("safe_import.file_select.prompt"), color=shell.COLORS["muted"], wrap=520)
        dpg.add_input_text(tag=PATH_INPUT, label=t("safe_import.file_select.path_label"), width=400)
        dpg.add_text("", tag=FILE_ERROR, color=shell.COLORS["error"], wrap=520, show=False)
        dpg.add_spacer(height=8)
        with dpg.group(horizontal=True):
            dpg.add_button(label=t("safe_import.file_select.browse"), width=110, callback=lambda: open_browse())
            dpg.add_button(
                label=t("safe_import.file_select.scan"),
                width=120,
                callback=lambda: shell.safe_import_scan_path(),
            )
            dpg.add_button(
                label=t("actions.cancel"),
                width=100,
                callback=lambda: _delete_if_exists(FILE_MODAL),
            )


def open_browse() -> None:
    _delete_if_exists(FILE_DIALOG)
    with dpg.file_dialog(
        tag=FILE_DIALOG,
        directory_selector=False,
        show=True,
        modal=True,
        width=600,
        height=420,
        callback=_on_file_picked,
    ):
        dpg.add_file_extension(".json", color=(120, 200, 160, 255))
        dpg.add_file_extension(".*")


def _on_file_picked(_sender, app_data) -> None:
    selection = ""
    if isinstance(app_data, dict):
        selection = app_data.get("file_path_name") or ""
    if selection and dpg.does_item_exist(PATH_INPUT):
        dpg.set_value(PATH_INPUT, selection)


def show_file_error(message: str) -> None:
    if dpg.does_item_exist(FILE_ERROR):
        dpg.set_value(FILE_ERROR, message)
        dpg.configure_item(FILE_ERROR, show=True)


# --- Step 2-5: preview (scan summary + checklist + diff + apply) ------------


def open_preview(shell) -> None:
    result: ImportResult | None = getattr(shell, "_safe_import_result", None)
    if result is None:
        return
    _delete_if_exists(FILE_MODAL)
    _delete_if_exists(PREVIEW_MODAL)
    with dpg.window(
        tag=PREVIEW_MODAL,
        label=t("safe_import.title"),
        modal=True,
        no_resize=True,
        width=760,
        height=660,
    ):
        dpg.add_text(t("safe_import.header"), wrap=720)
        dpg.add_text(t("safe_import.trust_statement"), color=shell.COLORS["muted"], wrap=720)
        dpg.add_spacer(height=8)

        if not result.ok:
            _render_failure(shell, result)
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label=t("actions.cancel"),
                    width=110,
                    callback=lambda: _delete_if_exists(PREVIEW_MODAL),
                )
            return

        _render_scan_summary(shell, result)
        dpg.add_spacer(height=8)
        _render_category_checklist(shell, result)
        dpg.add_spacer(height=8)
        _render_diff(shell, result)
        dpg.add_spacer(height=10)
        _render_apply_footer(shell, result)


def _render_failure(shell, result: ImportResult) -> None:
    reason = t(result.error_key) if result.error_key else ""
    dpg.add_text(
        t("safe_import.summary.failed", reason=reason),
        color=shell.COLORS["error"],
        wrap=720,
        tag="safe_import_failure_text",
    )
    if result.error_detail:
        dpg.add_text(result.error_detail, color=shell.COLORS["muted"], wrap=720)
    if result.blocked_fields:
        dpg.add_text(
            t("safe_import.summary.blocked_fields", count=len(result.blocked_fields)),
            color=shell.COLORS["muted"],
            wrap=720,
        )


def _render_scan_summary(shell, result: ImportResult) -> None:
    # Summary lines vary with the import (compatible + optional automation /
    # device-change / unknown-field lines + badges). Fit to content (DPG-2.x
    # auto_resize_y, legacy fill flag suppressed) so the summary never grows its
    # own inner scrollbar: the prior fixed 132 clipped a fully-populated summary
    # by 36px (tools/diag_dpg_card_clip.py). The diff list below keeps its own
    # bounded scroll region; only this fixed summary is converted.
    with dpg.child_window(
        border=True,
        tag="safe_import_summary_card",
        auto_resize_y=True,
        autosize_y=False,
    ):
        section_title(t("safe_import.summary.title"))
        badges.render_badges(
            badges.badges_for_import_result(result),
            tag_prefix="safe_import_summary",
        )
        dpg.add_text(t("safe_import.summary.compatible"), color=shell.COLORS["success"])
        if result.has_automation:
            dpg.add_text(
                t("safe_import.summary.automation_blocked", count=result.blocked_automation_count),
                color=shell.COLORS["muted"],
            )
        device_changes = len(result.categories.get(RiskCategory.DEVICE, []))
        if device_changes:
            dpg.add_text(
                t("safe_import.summary.device_changes", count=device_changes),
                color=shell.COLORS["warn"],
            )
        if result.unknown_fields:
            dpg.add_text(
                t("safe_import.summary.unknown_fields", count=len(result.unknown_fields)),
                color=shell.COLORS["warn"],
            )


def _render_category_checklist(shell, result: ImportResult) -> None:
    selected: set[RiskCategory] = getattr(shell, "_safe_import_selected", set())
    section_title(t("safe_import.categories.title"))
    for category in SELECTABLE_CATEGORIES:
        if not result.categories.get(category):
            continue
        dpg.add_checkbox(
            label=t(category_label_key(category)),
            default_value=category in selected,
            tag=f"safe_import_cat_{category.value}",
            user_data=category,
            callback=lambda _s, value, cat: shell.safe_import_toggle_category(cat, value),
        )
        if category is RiskCategory.DEVICE:
            dpg.add_text(
                t("safe_import.categories.device_hint"),
                color=shell.COLORS["warn"],
                wrap=700,
            )
    if result.has_automation:
        dpg.add_text(
            t("safe_import.categories.automation_blocked"),
            color=shell.COLORS["muted"],
            tag="safe_import_automation_blocked_label",
        )
        dpg.add_text(
            t("safe_import.categories.automation_hint"),
            color=shell.COLORS["muted"],
            wrap=700,
        )
    dpg.add_button(
        label=t("safe_import.categories.select_all"),
        callback=lambda: shell.safe_import_select_all(),
        tag="safe_import_select_all_button",
    )


def _render_diff(shell, result: ImportResult) -> None:
    section_title(t("safe_import.diff.title"))
    with dpg.child_window(height=210, border=True, tag="safe_import_diff_region"):
        for category in _DIFF_CATEGORY_ORDER:
            changes = result.categories.get(category)
            if not changes:
                continue
            is_device = category is RiskCategory.DEVICE
            header_color = shell.COLORS["warn"] if is_device else shell.COLORS["muted"]
            dpg.add_text(t(category_label_key(category)), color=header_color)
            for change in changes:
                row_color = shell.COLORS["warn"] if is_device else shell.COLORS["text"]
                current = summarize_value(change.key, change.current_value)
                imported = summarize_field(change)
                dpg.add_text(
                    f"  {t(change.label_key)}: {current} -> {imported}",
                    color=row_color,
                    wrap=700,
                )
                if change.key == "motion_settings":
                    # Motion is imported/preserved but the wrapper has no
                    # motion write path (restore registry: UNSUPPORTED,
                    # writable=False) — the preview must not imply an apply.
                    dpg.add_text(
                        f"    {t('safe_import.diff.motion_never_applied')}",
                        color=shell.COLORS["muted"],
                        wrap=700,
                    )
        if result.has_automation:
            dpg.add_text(
                f"  {t('safe_import.diff.blocked_label')}: "
                + ", ".join(result.blocked_fields),
                color=shell.COLORS["muted"],
                wrap=700,
            )
    with dpg.collapsing_header(label=t("safe_import.diff.advanced"), default_open=False):
        raw = json.dumps(snapshot_to_dict(result.profile.snapshot), indent=2)
        widget = dpg.add_input_text(
            multiline=True,
            readonly=True,
            default_value=raw,
            width=700,
            height=160,
            tag="safe_import_raw_json",
        )
        mono = font_for("mono", get_locale())
        if mono is not None and dpg.does_item_exist(mono):
            dpg.bind_item_font(widget, mono)


def _render_apply_footer(shell, result: ImportResult) -> None:
    dpg.add_input_text(
        tag=NAME_INPUT,
        label=t("safe_import.apply.name_label"),
        default_value=result.generated_name,
        width=360,
    )
    dpg.add_text(t("safe_import.apply.footer"), color=shell.COLORS["muted"], wrap=720)
    dpg.add_spacer(height=4)
    with dpg.group(horizontal=True):
        dpg.add_button(
            label=t("safe_import.apply.save_new"),
            width=190,
            callback=lambda: shell.safe_import_apply(apply_to_controller=False),
            tag="safe_import_save_new_button",
        )
        dpg.add_button(
            label=t("safe_import.apply.save_apply"),
            width=230,
            callback=lambda: shell.safe_import_request_apply_to_controller(),
            tag="safe_import_save_apply_button",
        )
        dpg.add_button(
            label=t("actions.cancel"),
            width=100,
            callback=lambda: _delete_if_exists(PREVIEW_MODAL),
            tag="safe_import_cancel_button",
        )


# --- Apply confirmation (Save + Apply, or any Device selection) -------------


def open_apply_confirm(shell) -> None:
    _delete_if_exists(CONFIRM_MODAL)
    # The preview underneath is HIDDEN while this modal is up (DPG never
    # renders stacked modals — shell._defer_modal_swap). Dismissing must
    # therefore re-show it: Cancel and the titlebar close both route
    # through shell.safe_import_cancel_apply_confirm, which swaps the
    # preview back; a bare delete here would strand it hidden.
    with dpg.window(
        tag=CONFIRM_MODAL,
        label=t("safe_import.apply.confirm_title"),
        modal=True,
        no_resize=True,
        width=480,
        height=190,
        on_close=lambda: shell.safe_import_cancel_apply_confirm(),
    ):
        dpg.add_text(t("safe_import.apply.confirm_body"), wrap=440)
        selected = getattr(shell, "_safe_import_selected", set())
        if RiskCategory.DEVICE in selected:
            dpg.add_spacer(height=4)
            dpg.add_text(
                t("safe_import.categories.device_hint"),
                color=shell.COLORS["warn"],
                wrap=440,
            )
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label=t("safe_import.apply.confirm_ok"),
                width=160,
                callback=lambda: shell.safe_import_apply(apply_to_controller=True),
                tag="safe_import_confirm_ok_button",
            )
            dpg.add_button(
                label=t("actions.cancel"),
                width=100,
                callback=lambda: shell.safe_import_cancel_apply_confirm(),
                tag="safe_import_confirm_cancel_button",
            )


# --- Step 6: result ---------------------------------------------------------


def open_result(shell) -> None:
    result: ImportResult | None = getattr(shell, "_safe_import_result", None)
    if result is None:
        return
    audit = result.audit
    _delete_if_exists(CONFIRM_MODAL)
    _delete_if_exists(PREVIEW_MODAL)
    _delete_if_exists(RESULT_MODAL)
    with dpg.window(
        tag=RESULT_MODAL,
        label=t("safe_import.result.title"),
        modal=True,
        no_resize=True,
        width=560,
        height=420,
    ):
        badges.render_badges(
            badges.badges_for_import_result(result),
            tag_prefix="safe_import_result",
        )
        dpg.add_spacer(height=6)
        dpg.add_text(t("safe_import.result.saved_as", name=result.generated_name))
        dpg.add_text(
            t("safe_import.result.profile_id", id=audit.generated_profile_id),
            color=shell.COLORS["muted"],
        )
        dpg.add_spacer(height=6)
        dpg.add_text(
            t("safe_import.result.imported_categories", categories=_category_names(audit.selected_categories))
        )
        dpg.add_text(
            t("safe_import.result.skipped_categories", categories=_category_names(audit.skipped_categories)),
            color=shell.COLORS["muted"],
        )
        if audit.blocked_field_names:
            dpg.add_text(
                t("safe_import.result.blocked_fields", fields=", ".join(audit.blocked_field_names)),
                color=shell.COLORS["muted"],
                wrap=520,
            )
        dpg.add_spacer(height=6)
        dpg.add_text(_write_status_text(audit), color=_write_status_color(shell, audit))
        if audit.restore_point_name:
            dpg.add_text(
                t("safe_import.result.restore_point", name=audit.restore_point_name),
                color=shell.COLORS["muted"],
                wrap=520,
            )
        dpg.add_spacer(height=10)
        dpg.add_button(
            label=t("safe_import.result.close"),
            width=120,
            callback=lambda: _delete_if_exists(RESULT_MODAL),
            tag="safe_import_result_close_button",
        )


def _category_names(categories) -> str:
    if not categories:
        return t("safe_import.result.none")
    return ", ".join(t(category_label_key(c)) for c in categories)


def _write_status_text(audit) -> str:
    if audit.controller_write == "verified":
        return t("safe_import.result.write_verified")
    if audit.controller_write == "sent":
        # "Sent" carries its reason when the apply flow recorded one: the
        # write burst was ACKed but read-back verification could not confirm
        # it (mismatch > unverifiable in display priority — a mismatch is the
        # actionable signal; the audit keeps both lists regardless).
        if audit.verify_read_failed:
            return t("safe_import.result.write_sent_verify_failed")
        if audit.verify_mismatched:
            return t(
                "safe_import.result.write_sent_mismatch",
                fields=", ".join(audit.verify_mismatched),
            )
        if audit.verify_unverifiable:
            return t(
                "safe_import.result.write_sent_unverifiable",
                count=len(audit.verify_unverifiable),
            )
        return t("safe_import.result.write_sent")
    return t("safe_import.result.write_not_performed")


def _write_status_color(shell, audit):
    if audit.controller_write == "verified":
        return shell.COLORS["success"]
    if audit.controller_write == "sent":
        return shell.COLORS["warn"]
    return shell.COLORS["muted"]
