"""Application preferences screen."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from zd_app.i18n import t
from zd_app.ui.components import card
from zd_app.ui.fonts import font_for
from zd_app.ui.typography import helper_text, screen_title, section_title


# Max width for the settings content column. The screen's root child fills the
# (wide) content region, which left a large blank horizontal field beside the
# narrow controls; bounding them in a single bordered card delineates the
# region so the controls read as an intentional column instead of stranded
# widgets. 720 comfortably holds the widest control (the bundle-dir input +
# its label) and fits the 1180px min-window content area with margin to spare.
_CONTENT_MAX_WIDTH = 720


# Canonical logging-verbosity values, in display order. The settings.json
# stores the canonical value (e.g. "Normal"); the combo shows the localized
# label via t(LOGGING_VERBOSITY_KEY_FOR[value]).
LOGGING_VERBOSITY_VALUES: tuple[str, ...] = ("Quiet", "Normal", "Verbose")
LOGGING_VERBOSITY_KEY_FOR: dict[str, str] = {
    "Quiet": "settings.logging_verbosity.quiet",
    "Normal": "settings.logging_verbosity.normal",
    "Verbose": "settings.logging_verbosity.verbose",
}


def build(shell, parent: str) -> None:
    settings = shell.settings
    language_items = [t("language.en"), t("language.zh-CN")]
    language_code_by_label = {
        t("language.en"): "en",
        t("language.zh-CN"): "zh-CN",
    }
    language_default = (
        t(f"language.{settings.language}")
        if settings.language in {"en", "zh-CN"}
        else t("language.en")
    )
    verbosity_labels = [t(LOGGING_VERBOSITY_KEY_FOR[v]) for v in LOGGING_VERBOSITY_VALUES]
    verbosity_value_by_label = {
        t(LOGGING_VERBOSITY_KEY_FOR[v]): v for v in LOGGING_VERBOSITY_VALUES
    }
    verbosity_default_label = t(
        LOGGING_VERBOSITY_KEY_FOR.get(settings.logging_verbosity, "settings.logging_verbosity.normal")
    )
    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        screen_title(t("preferences.title"))
        helper_text(t("preferences.subtitle"), wrap=620)
        dpg.add_spacer(height=12)

        # Bound the controls in a single max-width card so they form an
        # intentional column instead of stranding in the wide content region.
        with card(width=_CONTENT_MAX_WIDTH):
            language_combo_id = dpg.add_combo(
                items=language_items,
                default_value=language_default,
                label=t("settings.app.language_label"),
                width=240,
                callback=lambda _s, value: shell.update_language(
                    language_code_by_label.get(value, "en")
                ),
            )
            cjk_font = font_for("body", "zh-CN")
            if cjk_font is not None:
                dpg.bind_item_font(language_combo_id, cjk_font)
            verbosity_combo_id = dpg.add_combo(
                items=verbosity_labels,
                default_value=verbosity_default_label,
                label=t("settings.app.logging_verbosity_label"),
                width=240,
                tag="preferences_logging_verbosity_combo",
                callback=lambda _s, value: shell.update_setting(
                    "logging_verbosity",
                    verbosity_value_by_label.get(value, "Normal"),
                ),
            )
            # Bind the CJK font on the verbosity combo too so Mandarin labels
            # render correctly (Inter doesn't carry CJK glyphs).
            if cjk_font is not None:
                dpg.bind_item_font(verbosity_combo_id, cjk_font)
            dpg.add_checkbox(
                label=t("settings.app.auto_read_on_connect"),
                default_value=settings.auto_read_on_connect,
                callback=lambda _s, value: shell.update_setting("auto_read_on_connect", value),
            )
            dpg.add_input_text(
                default_value=settings.diagnostics_bundle_dir,
                label=t("settings.app.diagnostic_bundle_location"),
                width=420,
                callback=lambda _s, value: shell.update_setting("diagnostics_bundle_dir", value),
            )
            dpg.add_spacer(height=12)
            dpg.add_button(
                label=t("settings.app.restore_defaults"),
                width=190,
                callback=lambda: shell.restore_app_defaults(),
            )

            dpg.add_spacer(height=18)
            dpg.add_separator()
            dpg.add_spacer(height=12)
            section_title(
                t("developer.section_title"),
                tag="preferences_developer_section_title",
            )
            dpg.add_spacer(height=8)
            dpg.add_checkbox(
                label=t("developer.show_dev_panels.label"),
                default_value=settings.developer_panels_visible,
                tag="preferences_developer_panels_toggle",
                callback=lambda _s, value: _on_developer_panels_toggle(shell, value),
            )
            dpg.add_text(
                t("developer.show_dev_panels.helper"),
                color=shell.COLORS["muted"],
                wrap=620,
                tag="preferences_developer_panels_helper",
            )
            # Legacy-screens visibility is a navigation preference, not a
            # diagnostic — it used to live on the Diagnostics screen. It rides
            # the same "reveal normally-hidden surfaces" theme as the dev-panels
            # toggle, so it sits in the Developer section here.
            # ``_toggle_legacy_screens`` persists + rebuilds the full UI itself.
            dpg.add_spacer(height=12)
            dpg.add_checkbox(
                label=t("diagnostics.legacy.show_in_nav"),
                default_value=settings.show_legacy_screens,
                tag="preferences_show_legacy_screens_toggle",
                callback=lambda _s, value: shell._toggle_legacy_screens(value),
            )
            dpg.add_text(
                t("diagnostics.legacy.warning"),
                color=shell.COLORS["warn"],
                wrap=620,
                tag="preferences_show_legacy_screens_helper",
            )


def _on_developer_panels_toggle(shell, value: bool) -> None:
    """Persist the toggle then rebuild the UI so Diagnostics picks up the new state.

    Uses the existing ``update_setting`` (persists + logs + refresh_shell) plus
    ``rebuild_full_ui`` to force the Diagnostics screen to re-render with or
    without the dev-scaffold sections. Both methods are public on AppShell;
    this avoids adding a new shell-side method.
    """
    shell.update_setting("developer_panels_visible", bool(value))
    shell.rebuild_full_ui()
