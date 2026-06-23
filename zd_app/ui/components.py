"""Reusable layout components — cards, titled sections, and metric rows.

Dear PyGui has no CSS or cascade, so a "component" here is a small Python
builder that composes ``dpg`` primitives with the theme tokens (``themes.py``)
and the type-scale helpers (``typography.py``). The point is that every screen
gets the same panel chrome, section spacing, and label/value styling without
re-deciding padding, rounding, border color, and title sizing inline.

Four builders, meant to compose:

- ``card()``    -> a bordered, raised panel (context manager). Replaces ad-hoc
  ``dpg.child_window(border=True)`` blocks. Yields the child-window id.
- ``section()`` -> a ``section_title`` heading + a consistent gap, then the
  caller's content (context manager). ``card=True`` wraps it in a ``card()``.
- ``metric()``  -> a label + value stat row (muted label, prominent value).
- ``table()``   -> a themed data table (context manager). Replaces hand-rolled
  ``dpg.table`` + ``add_table_column`` blocks across the report / ledger /
  restore-point / profile screens with one chrome decision: tinted header,
  stronger header underline, subtle zebra rows, comfortable cell padding.
  Columns are declared as :class:`Column` specs; the caller still builds rows
  with ``dpg.table_row()``. Cell helpers — :func:`right_cell` (numeric/status
  columns) and :func:`action_button` (per-row actions, ``destructive=True`` for
  a red Delete-style button) — live alongside it, plus :func:`table_empty_state`
  for the "no rows yet" placeholder.

Card chrome is a bound theme (raised ``bg.card`` fill, ``border.strong``,
rounding, inner padding). It is built once at theme setup via
``register_card_theme()`` and looked up per card. If it was never registered
— e.g. headless unit tests that skip ``AppShell._setup_theme`` — ``card()``
skips the bind and the panel still renders with the global ChildBg/border.
This mirrors the font guard in ``typography.py`` / ``fonts.py``: bind only
when the resource is loaded, never require it. The table and destructive-button
themes (``register_table_theme()`` / ``register_destructive_theme()``) follow
the exact same register-once / guarded-bind pattern.

i18n: ``metric`` and ``card`` content render at body size through the globally
bound default font (``bind_default_font`` selects the locale's body font), so
CJK labels/values render correctly with no per-item font binding. Only the
``section`` title binds an explicit type-scale font (via ``section_title``,
which is itself locale-aware). Widths are intentionally never hard-coded here —
``card``/``section`` default to ``width=-1`` (fill the parent) so screens stay
responsive; callers that need a fixed size pass one explicitly.

Alignment guide: ``card()`` is the single card-internal left-alignment origin.
Its bound theme applies a uniform ``WindowPadding`` (``_CARD_PADDING``, from the
spacing scale), so every label / heading / control / table inside any card
starts at the same x-origin — the cross-screen consistency the UX panel flagged
as the fastest readability win. Screens should put block content inside
``card()`` (or ``section(card=True)``) rather than hand-rolling
``dpg.child_window`` panels, so they inherit that origin instead of the global
``WindowPadding``. Columnize only intentionally (e.g. side-by-side cards or a
``dpg.table``), never via ad-hoc per-row padding.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Union

import dearpygui.dearpygui as dpg

from zd_app.ui.themes import COLORS, SPACE_LG, SPACE_MD
from zd_app.ui.typography import section_title


logger = logging.getLogger(__name__)

ItemId = Union[int, str]

# Inner padding / corner rounding for the card surface. Padding comes from the
# spacing scale so cards share the app's vertical rhythm; rounding matches the
# global WindowRounding (6.0) so cards and top-level windows agree.
_CARD_PADDING = SPACE_LG
_CARD_ROUNDING = 6.0

# Height (px) to reserve below a fill-height list/table so a short PINNED footer
# (a 1-2 line muted caveat plus its SPACE_LG lead spacer) stays visible without
# the page (the autosize_y screen root) growing a SECOND scrollbar alongside the
# list's own. Used by screens whose dominant content is one long list/table with
# a caveat below it (restore_points, device_vs_profile): the list card takes
# ``card(height=-FOOTER_RESERVE_PX)`` so it fills the remaining height MINUS this
# reserve (ImGui negative-size semantics) and scrolls INTERNALLY on overflow,
# while the footer renders in the reserved band — one scrollbar, footer visible.
#
# Derivation (deliberate UPPER BOUND, not eyeballed): the shipped body faces
# (Inter / Noto Sans SC at 14px) render a ~19px line box — the repo's real-
# viewport card-clip bench measured the Wear-Ledger "heading + row + legend"
# block at ~146px. A caveat wraps to at most 2 lines at the screens' wrap<=900
# in both locales (en restore caveat 207 chars / zh 61 chars; ~128 en chars or
# ~64 zh chars per line). Reserve 3 lines for safety (3 x 19 = 57) + the
# SPACE_LG lead spacer (16) + slack = 88. Over-reserving only leaves harmless
# whitespace BELOW the footer; under-reserving re-introduces the page scroll,
# so erring high is correct. A real-viewport probe
# (tools/diag_dpg_content_scrollbar.py) is the final check.
FOOTER_RESERVE_PX = 88

# Built once by register_card_theme() at theme setup; None until then. card()
# binds it when present and still live, otherwise skips (headless guard).
_CARD_THEME: Optional[int] = None


def register_card_theme() -> Optional[int]:
    """Build and cache the card-surface theme; return its item id.

    Called once from ``AppShell._setup_theme`` (mirrors ``register_global_theme``
    and ``_build_nav_themes``). Safe to call again — it rebuilds and re-caches.
    Requires a live Dear PyGui context.
    """

    global _CARD_THEME
    with dpg.theme() as theme_id:
        with dpg.theme_component(dpg.mvChildWindow):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COLORS["bg.card"])
            dpg.add_theme_color(dpg.mvThemeCol_Border, COLORS["border.strong"])
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, _CARD_ROUNDING)
            dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1.0)
            dpg.add_theme_style(
                dpg.mvStyleVar_WindowPadding, _CARD_PADDING, _CARD_PADDING
            )
    _CARD_THEME = theme_id
    return theme_id


def reset_card_theme() -> None:
    """Forget the cached card theme (test hook).

    The suite's shared-context shim deletes every theme between tests, so a
    cached id can dangle (or be recycled onto another item). Tests that call
    :func:`register_card_theme` clear it again afterwards so the headless guard
    holds for the next test.
    """

    global _CARD_THEME
    _CARD_THEME = None


def _bind_card_theme(item: ItemId) -> None:
    """Bind the registered card theme to ``item`` when it is live.

    No-op when the theme was never registered (headless tests) or its id is
    stale — the test context shim deletes all themes between tests and Dear
    PyGui may recycle ids, so we verify the item still exists before binding.
    The card renders with the global ChildBg/border either way.
    """

    theme = _CARD_THEME
    if theme is None:
        return
    try:
        if not dpg.does_item_exist(theme):
            return
        dpg.bind_item_theme(item, theme)
    except Exception:  # pragma: no cover - cosmetic; never block a render
        logger.debug("Card theme bind skipped", exc_info=True)


@contextmanager
def card(
    *,
    width: int = -1,
    height: int = 0,
    border: bool = True,
    fit: bool = False,
    parent: ItemId = 0,
    tag: ItemId = 0,
    **child_window_kwargs,
) -> Iterator[ItemId]:
    """A bordered, raised panel — the standard container for a block of content.

    Context manager; yields the child-window id so callers can
    ``configure_item`` it later. ``width`` defaults to ``-1`` (fill the parent's
    content width) so cards stay responsive. Four height modes:

    - explicit ``height=N`` (N>0) -> a fixed panel (e.g. equal-height cards in
      a row).
    - explicit ``height=-N`` (N>0) -> fill the parent's remaining height MINUS
      ``N`` px (ImGui negative-size semantics). Use this to make a list/table
      card the single scroll surface while RESERVING ``N`` px below it for a
      pinned footer, so the page itself does not scroll (see
      :data:`FOOTER_RESERVE_PX`). The card scrolls INTERNALLY on overflow.
    - ``fit=True`` -> the card shrinks to fit its CONTENT height and never clips.
      Use this for a stacked card whose content varies (text/badges/buttons) so
      it can't grow an inner scrollbar.
    - default (``height=0``) -> the legacy ``autosize_y`` flag, which on Dear
      PyGui 2.x means "FILL the parent's remaining height" (ImGui ``size.y==0``),
      NOT "fit content". A default card therefore expands to fill its parent and
      scrolls internally if its content overflows — which is what the bounded
      scroll lists (restore-points table, wear-ledger event log) deliberately
      rely on. It is the WRONG choice for a stacked card that should fit; that is
      what ``fit=True`` is for. (DPG 2.x split content-fit into the separate
      ``auto_resize_y`` = ``ImGuiChildFlags_AutoResizeY``; see live_verify's
      ``_fit_card`` and tools/diag_dpg_card_clip.py, which measured the
      distinction on a real viewport.)

    Extra kwargs forward to ``dpg.child_window``.
    """

    kwargs = dict(child_window_kwargs)
    kwargs["width"] = width
    kwargs["border"] = border
    if height != 0:
        # height>0 -> fixed panel height. height<0 -> fill the parent's
        # remaining height MINUS abs(height) (ImGui negative-size: a footer
        # reserve). Either way an explicit height governs, so leave the legacy
        # autosize_y fill flag OFF.
        kwargs["height"] = height
    elif fit:
        # DPG-2.x content-fit: shrink to content, suppress the legacy fill flag.
        kwargs["auto_resize_y"] = True
        kwargs.setdefault("autosize_y", False)
    else:
        kwargs.setdefault("autosize_y", True)
    if parent:
        kwargs["parent"] = parent
    if tag:
        kwargs["tag"] = tag
    with dpg.child_window(**kwargs) as item:
        _bind_card_theme(item)
        yield item


# Internal alias so section() can open a card without the ``card`` bool keyword
# argument shadowing the builder name.
_open_card = card


@contextmanager
def section(
    title: str,
    *,
    card: bool = False,
    locale: Optional[str] = None,
    gap: int = SPACE_MD,
    title_tag: ItemId = 0,
    width: int = -1,
    height: int = 0,
    **card_kwargs,
) -> Iterator[Optional[ItemId]]:
    """A titled section: a ``section_title`` heading + a gap, then your content.

    Use inside an existing container, or pass ``card=True`` to wrap the whole
    section in a :func:`card` (``width`` / ``height`` / extra kwargs forward to
    the card). ``gap`` is the spacer below the title (set ``0`` to suppress).
    Yields the card id when ``card=True``, else ``None``.

    Sizing only applies with ``card=True``: with ``card=False`` there is no
    container to size — the heading is emitted inline into the caller's
    container — so ``width`` / ``height`` / forwarded card kwargs would be
    silently dropped. Passing them without ``card=True`` is a caller mistake
    and logs a warning rather than failing quietly (the section still renders).
    """

    title_kwargs: dict = {"locale": locale}
    if title_tag:
        title_kwargs["tag"] = title_tag

    def _emit_heading() -> None:
        section_title(title, **title_kwargs)
        if gap:
            dpg.add_spacer(height=gap)

    if card:
        with _open_card(width=width, height=height, **card_kwargs) as container:
            _emit_heading()
            yield container
    else:
        if width != -1 or height != 0 or card_kwargs:
            logger.warning(
                "section(%r): width/height/card kwargs are ignored unless "
                "card=True (no container to size when card=False); pass "
                "card=True to size the section.",
                title,
            )
        _emit_heading()
        yield None


def metric(
    label: str,
    value,
    *,
    value_color=None,
    label_color=None,
    value_tag: ItemId = 0,
    **value_kwargs,
) -> ItemId:
    """A label + value stat row (muted label, prominent value), laid out inline.

    Both render at body size through the bound default font, so CJK is handled
    without per-item binding. ``value`` is coerced to ``str``. Returns the value
    text item id — tag it via ``value_tag`` to update it later. The horizontal
    group's item spacing supplies the gap between label and value.
    """

    with dpg.group(horizontal=True):
        dpg.add_text(label, color=label_color or COLORS["text.secondary"])
        kwargs = dict(value_kwargs)
        kwargs["color"] = value_color or COLORS["text.primary"]
        if value_tag:
            kwargs["tag"] = value_tag
        return dpg.add_text(str(value), **kwargs)


# ---------------------------------------------------------------------------
# Table: the shared data-table builder + its themes and cell helpers.
#
# Five screens hand-rolled the same ``dpg.table`` + ``add_table_column`` block
# with slightly different chrome (some forgot zebra rows, the legacy profiles
# table had no row background at all). ``table()`` makes the chrome one
# decision: a tinted header with a stronger underline, subtle zebra rows, and
# comfortable cell padding, all from a bound theme. Columns are declared up
# front as :class:`Column` specs so width / weight / fixed-width intent reads
# at the call site; rows stay caller-built (``with dpg.table_row():``) because
# their contents are wildly heterogeneous (text, selectables, status chips,
# badge groups, action buttons).
# ---------------------------------------------------------------------------

# Table cell padding (px). Wider than the global ItemSpacing (8) so columns get
# horizontal breathing room, and tall enough that a single-line row clears the
# ~44px comfortable-density floor the UX pass asked for (body text ~18px tall +
# 2×12 vertical padding ≈ 42px; multi-line rows grow from there).
_TABLE_CELL_PAD_X = 10
_TABLE_CELL_PAD_Y = 12

# Built once by register_table_theme() / register_destructive_theme() at theme
# setup; None until then. The builders bind them only when present and still
# live, otherwise skip (headless guard) — same contract as _CARD_THEME.
_TABLE_THEME: Optional[int] = None
_DESTRUCTIVE_THEME: Optional[int] = None


@dataclass(frozen=True)
class Column:
    """One column spec for :func:`table`.

    - ``label``    -> header text (``""`` for an unlabeled column, e.g. actions).
    - ``weight``   -> stretch weight (Dear PyGui ``init_width_or_weight`` as a
      float) under the default proportional sizing policy. Mutually exclusive
      with ``width``.
    - ``width``    -> fixed pixel width (sets ``width_fixed=True``). Use for an
      actions column whose buttons must never be clipped.
    - ``numeric``  -> hint that the column holds numeric / status values; pair
      with :func:`right_cell` at the call site (best-effort right-align — see
      that helper for the Dear PyGui limitation).
    - ``no_resize`` -> pin the column width (no user drag-resize).
    """

    label: str = ""
    weight: Optional[float] = None
    width: Optional[int] = None
    numeric: bool = False
    no_resize: bool = False


# A bare string is accepted as shorthand for Column(label=...).
ColumnSpec = Union[Column, str]


def register_table_theme() -> Optional[int]:
    """Build and cache the data-table theme; return its item id.

    Called once from ``AppShell._setup_theme`` (alongside
    ``register_card_theme``). Requires a live Dear PyGui context — the
    ``mvTable`` / ``mvThemeCol_Table*`` constants only resolve under one, so
    every constant reference stays inside this function (never at module top
    level), mirroring ``register_card_theme`` / ``register_global_theme``.
    Safe to call again — it rebuilds and re-caches.
    """

    global _TABLE_THEME
    with dpg.theme() as theme_id:
        with dpg.theme_component(dpg.mvTable):
            # Header: a subtle tint one step above the card surface + a stronger
            # outer/underline border so the header row reads as a header. (Dear
            # PyGui renders native header labels in the bound default font with
            # no per-cell weight control, so "semibold header" is approximated
            # by the tint + underline, not an actual heavier font — a documented
            # platform limitation, like the right-align note in right_cell.)
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, COLORS["bg.raised"])
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, COLORS["border.strong"])
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, COLORS["border.subtle"])
            # Zebra: transparent base row + a faint translucent-white overlay on
            # alternate rows, so the striping adapts to whatever container the
            # table sits in (card bg, surface bg, content region) instead of a
            # fixed opaque colour that only looks right on one of them.
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (255, 255, 255, 12))
            dpg.add_theme_style(
                dpg.mvStyleVar_CellPadding, _TABLE_CELL_PAD_X, _TABLE_CELL_PAD_Y
            )
    _TABLE_THEME = theme_id
    return theme_id


def register_destructive_theme() -> Optional[int]:
    """Build and cache the destructive-action button theme; return its item id.

    A muted-red button that brightens toward ``error`` on hover, so a Delete
    action reads as distinct from neutral View / Apply / Restore actions
    (:func:`action_button` binds it when ``destructive=True``). Called once from
    ``AppShell._setup_theme``; same context requirement as
    :func:`register_table_theme`.
    """

    global _DESTRUCTIVE_THEME
    er, eg, eb, _ = COLORS["error"]

    def _scaled(factor: float) -> tuple:
        # Darkened shades of the error red — keeps the destructive palette tied
        # to the theme token instead of three free-floating magic tuples.
        return (int(er * factor), int(eg * factor), int(eb * factor), 255)

    with dpg.theme() as theme_id:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, _scaled(0.32))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _scaled(0.52))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _scaled(0.72))
            # Near-white text stays legible across the muted-to-bright red range.
            dpg.add_theme_color(dpg.mvThemeCol_Text, (250, 224, 224, 255))
    _DESTRUCTIVE_THEME = theme_id
    return theme_id


def reset_table_theme() -> None:
    """Forget the cached table theme (test hook; mirrors ``reset_card_theme``)."""

    global _TABLE_THEME
    _TABLE_THEME = None


def reset_destructive_theme() -> None:
    """Forget the cached destructive-button theme (test hook)."""

    global _DESTRUCTIVE_THEME
    _DESTRUCTIVE_THEME = None


def _bind_table_theme(item: ItemId) -> None:
    """Bind the registered table theme to ``item`` when it is live (else no-op).

    Same headless / stale-id guard as :func:`_bind_card_theme`: the table still
    renders with the global table colours when no theme was registered (e.g.
    unit tests that skip ``_setup_theme``) or its cached id was deleted by the
    test context shim.
    """

    _bind_optional_theme(item, _TABLE_THEME, "Table")


def _bind_destructive_theme(item: ItemId) -> None:
    """Bind the destructive-button theme to ``item`` when it is live (else no-op)."""

    _bind_optional_theme(item, _DESTRUCTIVE_THEME, "Destructive")


def _bind_optional_theme(item: ItemId, theme: Optional[int], what: str) -> None:
    """Shared guarded bind for the optional component themes.

    No-op when ``theme`` was never registered or its id is stale (the test
    context shim deletes all themes between tests and Dear PyGui may recycle
    ids, so verify the item still exists before binding). Cosmetic failures are
    swallowed — never block a render. Factored out of the per-theme binders so
    the guard logic lives in exactly one place.
    """

    if theme is None:
        return
    try:
        if not dpg.does_item_exist(theme):
            return
        dpg.bind_item_theme(item, theme)
    except Exception:  # pragma: no cover - cosmetic; never block a render
        logger.debug("%s theme bind skipped", what, exc_info=True)


@contextmanager
def table(
    columns: Iterable[ColumnSpec],
    *,
    tag: ItemId = 0,
    parent: ItemId = 0,
    resizable: bool = False,
    height: int = 0,
    scroll_y: bool = False,
    **table_kwargs,
) -> Iterator[ItemId]:
    """A themed data table: standard header / zebra / padding chrome, your rows.

    Context manager mirroring :func:`card` / :func:`section`. Opens a
    ``dpg.table`` with the app's standard chrome (header row, alternating row
    backgrounds, inner-horizontal + outer-horizontal borders, proportional
    sizing), binds the table theme, emits the header columns from ``columns``,
    then yields the table id so the caller adds rows exactly as before::

        with table([Column("Name", weight=2.0), Column("State", weight=1.0)]):
            for row in rows:
                with dpg.table_row():
                    dpg.add_text(row.name)
                    dpg.add_text(row.state)

    ``columns`` is a sequence of :class:`Column` specs (a bare ``str`` is
    shorthand for ``Column(label=str)``). The chrome defaults below can each be
    overridden per call through ``**table_kwargs`` — the legacy profiles table
    passes nothing extra; a call could pass ``borders_outerH=False`` etc. if a
    site needs to opt out. ``resizable`` / ``height`` / ``scroll_y`` are surfaced
    as explicit kwargs because several call sites use them.
    """

    cols = [c if isinstance(c, Column) else Column(label=str(c)) for c in columns]

    kwargs = {
        "header_row": True,
        "row_background": True,
        "borders_innerH": True,
        "borders_outerH": True,
        # Vertical inner borders off by default: the existing tables read as
        # row-striped lists, not spreadsheets. A site can flip it via kwargs.
        "borders_innerV": False,
        "policy": dpg.mvTable_SizingStretchProp,
    }
    kwargs.update(table_kwargs)
    if tag:
        kwargs["tag"] = tag
    if parent:
        kwargs["parent"] = parent
    if resizable:
        kwargs["resizable"] = True
    if height > 0:
        kwargs["height"] = height
    if scroll_y:
        kwargs["scroll_y"] = True

    with dpg.table(**kwargs) as table_id:
        _bind_table_theme(table_id)
        for col in cols:
            _add_table_column(col)
        yield table_id


def _add_table_column(col: Column) -> None:
    """Emit one ``dpg.add_table_column`` from a :class:`Column` spec."""

    col_kwargs: dict = {}
    if col.label:
        col_kwargs["label"] = col.label
    if col.width is not None:
        # Fixed-width column: width_fixed + the pixel width as init_width.
        col_kwargs["width_fixed"] = True
        col_kwargs["init_width_or_weight"] = col.width
    elif col.weight is not None:
        col_kwargs["init_width_or_weight"] = col.weight
    if col.no_resize:
        col_kwargs["no_resize"] = True
    dpg.add_table_column(**col_kwargs)


def right_cell(value, *, color=None, **text_kwargs) -> ItemId:
    """Render a value cell intended for a numeric / status (right-aligned) column.

    The UX pass asked for numeric and status columns to be right-aligned, but
    Dear PyGui tables expose **no per-cell horizontal alignment**, and under the
    proportional sizing policy a column's pixel width isn't known until render
    time — so true pixel right-alignment isn't achievable at build time. This
    helper is the single place that owns that intent: today it renders the value
    at the cell's left origin (functionally a themed ``dpg.add_text``), in
    primary text colour, so a future Dear PyGui that gains cell alignment (or a
    fixed-width column we can measure) changes one function instead of every
    call site. Mirrors the "bind only what the platform supports, note the
    limitation" guards used in :func:`card` and ``typography``.
    """

    kwargs = dict(text_kwargs)
    kwargs["color"] = color if color is not None else COLORS["text.primary"]
    return dpg.add_text(str(value), **kwargs)


def action_button(label: str, *, destructive: bool = False, **button_kwargs) -> ItemId:
    """A per-row action button; ``destructive=True`` binds the red Delete theme.

    Thin wrapper over ``dpg.add_button`` so a row's actions share one styling
    decision: neutral actions (View / Apply / Restore / Export) render with the
    global button theme, and a destructive action (Delete) gets the muted-red
    :func:`register_destructive_theme` so it reads as distinct without needing a
    confirmation flow here (the screens own those). Forwards every other kwarg
    (``callback`` / ``user_data`` / ``small`` / ``width`` …) to ``dpg.add_button``
    untouched, so existing callback signatures are preserved. Returns the button
    id. The destructive bind is the same guarded no-op as the table theme when
    unregistered (headless tests).
    """

    item = dpg.add_button(label=label, **button_kwargs)
    if destructive:
        _bind_destructive_theme(item)
    return item


def table_empty_state(message: str, *, tag: ItemId = 0) -> ItemId:
    """A clean "no rows yet" placeholder for an empty / sparse table.

    Use in a screen's existing ``if not rows:`` branch instead of a bare
    ``dpg.add_text``, so low-content screens (the Wear Ledger especially) read
    as intentionally-empty rather than broken. Renders the message in muted text
    with a small leading gap for breathing room; returns the text item id.
    """

    dpg.add_spacer(height=SPACE_MD)
    kwargs: dict = {"color": COLORS["text.muted"], "wrap": 900}
    if tag:
        kwargs["tag"] = tag
    return dpg.add_text(message, **kwargs)
