"""About screen."""

from __future__ import annotations

import logging
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import dearpygui.dearpygui as dpg

from zd_app.i18n import get_locale, t
from zd_app.ui.components import card
from zd_app.ui.fonts import font_for
from zd_app.ui.typography import screen_title, section_title
from zd_app.version import __build_commit__, __build_date__, __version__


logger = logging.getLogger(__name__)


# Max width for the About content column. The screen's content is left-aligned
# and ~620px wide inside a much wider content region, which reads as stranded;
# bounding it in a single bordered card delineates the identity/security block
# as an intentional panel. 720 fits the 1180px min-window content area.
_CONTENT_MAX_WIDTH = 720


# Tag of the in-app bundled-license modal. Named because three places need it
# (the seam's teardown list, the create, the Close button) and because the
# sibling screen modules all address their modals through a module constant.
LICENSE_MODAL = "about_license_modal"


# Repo + issue-tracker URLs. Buttons render only when their URL is non-None.
# LICENSE_URL stays None: the Licenses button opens the in-app license modal,
# not a URL.
REPO_URL: Optional[str] = "https://github.com/EvilHumphrey/LegendCTL"
ISSUE_URL: Optional[str] = "https://github.com/EvilHumphrey/LegendCTL/issues"
LICENSE_URL: Optional[str] = None


# Bundled third-party deps shown in the license modal. Order matters:
# the project section renders first and is expanded by default; the rest
# stay collapsed so the modal does not become a wall of text.
LICENSE_DEPS: tuple[tuple[str, str, str], ...] = (
    # (slug, spdx, header_key)
    ("project", "MIT", "about.license_modal.dep_header.project"),
    ("python", "PSF-2.0", "about.license_modal.dep_header.python"),
    ("dpg", "MIT", "about.license_modal.dep_header.dpg"),
    ("inter", "OFL-1.1", "about.license_modal.dep_header.inter"),
    ("jetbrains-mono", "OFL-1.1", "about.license_modal.dep_header.jetbrains_mono"),
    ("noto-sans-sc", "OFL-1.1", "about.license_modal.dep_header.noto_sans_sc"),
)


def _licenses_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "assets" / "licenses"
    return Path(__file__).resolve().parents[3] / "assets" / "licenses"


def _read_license(slug: str) -> str:
    path = _licenses_dir() / f"{slug}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("License file missing: %s", path)
        return t("about.license_modal.missing")


def build(shell, parent: str) -> None:
    with dpg.child_window(parent=parent, autosize_x=True, autosize_y=True, border=False):
        dpg.add_spacer(height=16)

        # Bound the identity + security content in one max-width card so it
        # reads as an intentional panel rather than left-stranded text in a
        # wide empty field.
        with card(width=_CONTENT_MAX_WIDTH):
            with dpg.drawlist(width=96, height=96):
                # Live-deadzone radar mark — the app's signature Live Verify
                # visual distilled: faint rings + crosshair, one accent ring with
                # the stick vector and blip sitting on it, a light center dot.
                ring = shell.COLORS["panel_alt"]
                dpg.draw_line((10, 48), (86, 48), color=ring, thickness=1)
                dpg.draw_line((48, 10), (48, 86), color=ring, thickness=1)
                dpg.draw_circle((48, 48), 16, color=ring, thickness=1)
                dpg.draw_circle((48, 48), 28, color=ring, thickness=1)
                dpg.draw_circle((48, 48), 38, color=shell.COLORS["accent"], thickness=2.5)
                dpg.draw_line((48, 48), (79, 26), color=shell.COLORS["accent"], thickness=2.5)
                dpg.draw_circle(
                    (79, 26), 5,
                    color=shell.COLORS["accent_hover"],
                    fill=shell.COLORS["accent_hover"],
                )
                dpg.draw_circle(
                    (48, 48), 3,
                    color=shell.COLORS["text"],
                    fill=shell.COLORS["text"],
                )
            dpg.add_spacer(height=14)
            screen_title(t("about.app_name"), tag="about_app_name")
            version_line = f"{t('about.version_label')} {__version__}"
            if __build_commit__:
                version_line = f"{version_line}   {t('about.commit_label')} {__build_commit__}"
            if __build_date__:
                version_line = f"{version_line}   {t('about.build_date_label')} {__build_date__}"
            dpg.add_text(version_line, color=shell.COLORS["muted"], tag="about_version_line")

            dpg.add_spacer(height=18)
            dpg.add_text(t("about.tagline"), wrap=620, tag="about_tagline")

            # ZD-required unaffiliated / warranty disclaimer. This is a legal
            # denial (the project is not affiliated with ZD Gaming), in the same
            # whitelisted spirit as the per-subsystem claim-boundary paragraphs
            # in services/*/boundary.py — render it verbatim, do not paraphrase.
            # Default (primary) text color, not muted, so it reads as a
            # clearly-visible notice rather than fine print.
            dpg.add_spacer(height=14)
            dpg.add_text(
                t("about.zd_disclaimer"),
                wrap=620,
                tag="about_zd_disclaimer",
            )

            dpg.add_spacer(height=16)
            with dpg.group(horizontal=True):
                if REPO_URL:
                    dpg.add_button(
                        label=t("about.repo_link"),
                        width=130,
                        tag="about_btn_repo",
                        callback=lambda: _open_repo_url(),
                    )
                dpg.add_button(
                    label=t("about.license_link"),
                    width=130,
                    tag="about_btn_licenses",
                    callback=lambda: _open_license_view(shell),
                )
                if ISSUE_URL:
                    dpg.add_button(
                        label=t("about.report_issue"),
                        width=130,
                        tag="about_btn_issue",
                        callback=lambda: _open_issue_url(),
                    )

            dpg.add_spacer(height=18)
            dpg.add_text(
                t("about.disclaimer"),
                color=shell.COLORS["muted"],
                wrap=620,
                tag="about_disclaimer",
            )

            dpg.add_spacer(height=18)
            section_title(
                t("about.security.title"),
                tag="about_security_title",
            )
            dpg.add_text(
                t("safe_import.trust_statement"),
                wrap=620,
                tag="about_security_trust_statement",
            )

            dpg.add_spacer(height=14)
            dpg.add_text(
                t("about.footer.copyright"),
                color=shell.COLORS["muted"],
                tag="about_footer_copyright",
            )


def _open_repo_url() -> None:
    if REPO_URL:
        webbrowser.open(REPO_URL)


def _open_issue_url() -> None:
    if ISSUE_URL:
        webbrowser.open(ISSUE_URL)


def _open_license_view(shell) -> None:
    """Open the bundled-license modal (About -> Licenses).

    Modal-swap hop (2026-07-27): this used to build the window inline, right
    after ``delete_item(LICENSE_MODAL)`` — a teardown and a create in ONE
    render pass. Under the benched DPG modal law (2026-06-11,
    ``tools/diag_dpg_modal_thread_visibility.py``) the fresh modal then exists
    but never becomes interactive: the Close button is dead, no exception is
    raised and nothing is logged. Routing through ``shell._defer_modal_swap``
    puts a rendered frame between teardown and create, which is what cures it.

    Reachable by the plainest path there is. The window omits ``no_close``,
    which DearPyGui passes straight through as ``no_close=False`` (there is no
    "unset" sentinel), so the titlebar X is present — and the X only HIDES a
    window, it never deletes it. Licenses -> X -> Licenses again is therefore
    delete+create in one pass, and the resulting modal has a dead Close button
    whose only remaining exit is the X that hides it again.

    The ``getattr`` guard mirrors
    :func:`zd_app.ui.screens.modules._open_zip_preview`: the mocked suite hands
    screens lightweight fake shells that carry no seam, and the inline branch
    preserves the synchronous contract those paths rely on.

    Coalescing policy — plain drop-first, deliberately no payload parking.
    This site takes no per-request argument: every click asks for the same
    modal, built from module-level ``LICENSE_DEPS`` and the on-disk license
    files, so a request dropped inside the pending window is indistinguishable
    from the one that opens. (Contrast the Modules assign/notes modals, which
    park a side on the screen state because a dropped request there would open
    the wrong module's modal.)

    Screen-scoped: the Licenses button lives on About, so a request made a
    frame before the user clicks a nav item must not land on top of the
    destination. Without ``owner_screen`` the seam has no owner to compare
    against and ``switch_screen`` cannot cancel it.
    """

    def _open() -> None:
        # Resolved at create time, alongside the widgets it binds: the font
        # registry is DPG state, and there is nothing to gain from reading it
        # a frame early.
        mono_font = font_for("mono", get_locale())
        with dpg.window(
            tag=LICENSE_MODAL,
            label=t("about.license_modal.title"),
            modal=True,
            width=720,
            height=600,
            no_resize=True,
        ):
            dpg.add_text(
                t("about.license_modal.section_intro"),
                wrap=660,
                color=shell.COLORS["muted"],
                tag="about_license_intro",
            )
            dpg.add_spacer(height=8)
            for idx, (slug, spdx, header_key) in enumerate(LICENSE_DEPS):
                section_tag = f"about_license_section_{slug.replace('-', '_')}"
                with dpg.collapsing_header(
                    label=t(header_key),
                    default_open=(idx == 0),
                    tag=section_tag,
                ):
                    dpg.add_text(
                        f"{t('about.license_modal.spdx_label')} {spdx}",
                        color=shell.COLORS["muted"],
                    )
                    dpg.add_spacer(height=4)
                    license_text = _read_license(slug)
                    license_widget = dpg.add_text(license_text, wrap=660)
                    if mono_font is not None:
                        dpg.bind_item_font(license_widget, mono_font)
            dpg.add_spacer(height=8)
            dpg.add_button(
                label=t("about.license_modal.close"),
                width=110,
                tag="about_license_close_btn",
                callback=lambda: dpg.delete_item(LICENSE_MODAL),
            )

    modal_swap = getattr(shell, "_defer_modal_swap", None)
    if callable(modal_swap):
        modal_swap(
            _open,
            delete_tags=(LICENSE_MODAL,),
            key="about_license_view",
            owner_screen="about",
        )
        return

    if dpg.does_item_exist(LICENSE_MODAL):
        dpg.delete_item(LICENSE_MODAL)
    _open()
