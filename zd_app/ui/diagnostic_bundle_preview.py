"""Diagnostic bundle share-preview modal."""

from __future__ import annotations

from typing import Callable

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.services.diagnostic_bundle import (
    DiagnosticBundlePreviewItem,
    DiagnosticBundlePreviewManifest,
)


PREVIEW_MODAL_TAG = "diagnostic_bundle_preview_modal"
PREVIEW_MODAL_WIDTH = 860
PREVIEW_MODAL_HEIGHT = 640
PREVIEW_WRAP_WIDTH = 800
PREVIEW_CONTENT_HEIGHT = 430


def _modal_child_tag(tag: str, suffix: str) -> str:
    return f"{tag}__{suffix}"


def close_preview_modal(tag: str = PREVIEW_MODAL_TAG) -> None:
    try:
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
    except SystemError:
        pass


def open_preview_modal(
    shell,
    manifest: DiagnosticBundlePreviewManifest,
    *,
    on_export: Callable[[], None],
    on_cancel: Callable[[], None] | None = None,
    tag: str = PREVIEW_MODAL_TAG,
) -> None:
    """Render the pre-export manifest with Export / Cancel actions."""

    close_preview_modal(tag)
    colors = getattr(shell, "COLORS", {})
    muted = colors.get("muted", (150, 150, 150, 255))
    text = colors.get("text", (230, 230, 230, 255))
    accent = colors.get("accent", text)
    resolved = False

    def _export() -> None:
        nonlocal resolved
        if resolved:
            return
        resolved = True
        close_preview_modal(tag)
        on_export()

    def _cancel() -> None:
        nonlocal resolved
        if resolved:
            return
        resolved = True
        close_preview_modal(tag)
        if on_cancel is not None:
            on_cancel()

    window_kwargs = {
        "tag": tag,
        "label": t("diagnostics.bundle_preview.title"),
        "modal": True,
        "no_close": False,
        "no_resize": True,
        "no_scrollbar": True,
        "width": PREVIEW_MODAL_WIDTH,
        "height": PREVIEW_MODAL_HEIGHT,
        "on_close": lambda *_a: _cancel(),
    }
    position = _center_modal_position(PREVIEW_MODAL_WIDTH, PREVIEW_MODAL_HEIGHT)
    if position is not None:
        window_kwargs["pos"] = position

    with dpg.window(**window_kwargs):
        dpg.add_text(
            t("diagnostics.bundle_preview.summary"),
            color=accent,
            tag=_modal_child_tag(tag, "summary"),
            wrap=PREVIEW_WRAP_WIDTH,
        )
        dpg.add_spacer(height=8)
        with dpg.child_window(
            tag=_modal_child_tag(tag, "content_region"),
            width=-1,
            height=PREVIEW_CONTENT_HEIGHT,
            border=True,
        ):
            dpg.add_text(
                t("diagnostics.bundle_preview.contents_heading"),
                color=text,
            )
            dpg.add_spacer(height=4)
            for item in manifest.items:
                _render_item(item, muted=muted, text=text)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=6)
            dpg.add_text(
                t("diagnostics.bundle_preview.excluded_heading"),
                color=text,
            )
            dpg.add_spacer(height=4)
            for item in manifest.excluded_items:
                _render_item(item, muted=muted, text=text, excluded=True)

        dpg.add_spacer(height=10)
        dpg.add_separator()
        dpg.add_spacer(height=6)
        with dpg.group(
            horizontal=True,
            tag=_modal_child_tag(tag, "actions"),
        ):
            dpg.add_button(
                label=t("diagnostics.bundle_preview.export_button"),
                tag=_modal_child_tag(tag, "export_button"),
                width=140,
                callback=lambda *_a: _export(),
            )
            dpg.add_button(
                label=t("diagnostics.bundle_preview.cancel_button"),
                tag=_modal_child_tag(tag, "cancel_button"),
                width=120,
                callback=lambda *_a: _cancel(),
            )


def _center_modal_position(width: int, height: int) -> tuple[int, int] | None:
    try:
        viewport_width = int(dpg.get_viewport_client_width())
        viewport_height = int(dpg.get_viewport_client_height())
    except Exception:  # noqa: BLE001 - tests may create only a DPG context.
        try:
            viewport_width = int(dpg.get_viewport_width())
            viewport_height = int(dpg.get_viewport_height())
        except Exception:  # noqa: BLE001
            return None
    if viewport_width <= 0 or viewport_height <= 0:
        return None
    return (
        max(20, (viewport_width - width) // 2),
        max(20, (viewport_height - height) // 2),
    )


def _render_item(
    item: DiagnosticBundlePreviewItem,
    *,
    muted,
    text,
    excluded: bool = False,
) -> None:
    label = t(f"diagnostics.bundle_preview.{item.key}.label")
    heading = (
        t("diagnostics.bundle_preview.excluded_count", label=label)
        if excluded
        else t(
            "diagnostics.bundle_preview.included_count",
            label=label,
            count=item.count,
        )
    )
    dpg.add_text(heading, color=text, wrap=660)
    dpg.add_text(_scope_text(item), color=muted, wrap=660)
    dpg.add_text(_privacy_text(item), color=muted, wrap=660)
    dpg.add_spacer(height=6)


def _scope_text(item: DiagnosticBundlePreviewItem) -> str:
    metadata = item.metadata
    if item.key == "module_passports":
        return t(
            "diagnostics.bundle_preview.module_passports.scope",
            active_count=int(metadata.get("active_count", 0)),
            archived_count=int(metadata.get("archived_count", 0)),
            member_count=item.member_count,
        )
    if item.key == "health_reports":
        return t(
            "diagnostics.bundle_preview.health_reports.scope",
            report_count=item.count,
            markdown_count=int(metadata.get("markdown_count", 0)),
            json_count=int(metadata.get("json_count", 0)),
            member_count=item.member_count,
        )
    if item.key == "wear_ledger":
        return t(
            "diagnostics.bundle_preview.wear_ledger.scope",
            window_days=int(metadata.get("window_days", 0)),
            event_count=int(metadata.get("event_count", 0)),
            service_note_count=int(metadata.get("service_note_count", 0)),
        )
    return t(
        f"diagnostics.bundle_preview.{item.key}.scope",
        member_count=item.member_count,
    )


def _privacy_text(item: DiagnosticBundlePreviewItem) -> str:
    return t(
        "diagnostics.bundle_preview.privacy_line",
        posture=t(f"diagnostics.bundle_preview.{item.key}.privacy"),
    )
