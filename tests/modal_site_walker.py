"""Static discovery of every Dear PyGui window-family construction in ``zd_app``.

Why this module exists
----------------------
Dear PyGui has a benched law (2026-06-11, matrix in
``tools/diag_dpg_modal_thread_visibility.py``, encoded by
``AppShell._defer_modal_swap``): a modal window created in the same render pass
in which another modal was showing -- deleted, hidden, or simply left up --
exists with all its children but never becomes interactive. No exception is
raised, nothing is logged, and a mocked test cannot observe it because the
symptom lives in the native renderer.

The only defence that scales is *enumeration*: know every construction site, and
know for each one whether it is on the seam. Six hand sweeps each believed they
had the full list and each was wrong -- the misses were a ``dpg.popup(`` with no
``modal=`` kwarg on the call, a construction whose ``modal`` key arrives inside a
``**kwargs`` dict, and a defect reachable only by tracing callers. So this
walker deliberately does NOT key off a single syntactic pattern:

* it matches the whole window family (``window`` / ``add_window`` / ``popup`` /
  ``add_popup`` / ``file_dialog`` / ``add_file_dialog``), not just calls that
  spell ``modal=True``;
* it resolves tags through module constants, cross-module attribute constants,
  parameter defaults and locally-built kwargs dicts;
* anything it cannot decide statically is reported as
  :data:`Modality.UNDECIDABLE` or with ``tag=None`` rather than skipped, so the
  registry gate fails loudly and a human has to classify it.

A gate that silently skips the thing it exists to find is worse than no gate,
so every branch here that cannot reach a confident answer degrades to "loud",
never to "clean".

The registry that consumes this walker lives in
``tests/test_modal_site_registry.py``.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "zd_app"

# The seam every modal-opening flow is supposed to route through.
SEAM_ATTR = "_defer_modal_swap"

# Constructors that produce a window-family item. ``window`` and ``file_dialog``
# are modal only when asked; ``popup`` is always an ImGui popup and therefore
# always competes for the popup slot (see ``no_open_over_existing_popup``), so
# it is tracked unconditionally.
WINDOW_CONSTRUCTORS = frozenset(
    {
        "window",
        "add_window",
        "file_dialog",
        "add_file_dialog",
    }
)
POPUP_CONSTRUCTORS = frozenset({"popup", "add_popup"})
WINDOW_FAMILY = WINDOW_CONSTRUCTORS | POPUP_CONSTRUCTORS

# Import forms the alias resolver understands. Any other way of getting at the
# Dear PyGui module is a discovery hole, so it is surfaced as a finding.
_KNOWN_DPG_MODULE = "dearpygui.dearpygui"


class Modality(str, Enum):
    """How a discovered construction relates to the modal slot."""

    MODAL = "MODAL"  # modal=True, statically proven
    POPUP = "POPUP"  # dpg.popup family: always an ImGui popup
    NON_MODAL = "NON_MODAL"  # modal absent or modal=False, statically proven
    UNDECIDABLE = "UNDECIDABLE"  # modality cannot be decided from source


class TagSource(str, Enum):
    """How the walker arrived at a site's tag, if at all."""

    LITERAL = "LITERAL"  # tag="literal_string"
    MODULE_CONST = "MODULE_CONST"  # tag=MODULE_LEVEL_NAME
    IMPORTED_CONST = "IMPORTED_CONST"  # tag=other_module.NAME
    PARAM_DEFAULT = "PARAM_DEFAULT"  # tag=<parameter whose default is a const>
    KWARGS_DICT = "KWARGS_DICT"  # dpg.window(**locally_built_dict)
    POSITIONAL = "POSITIONAL"  # popup(parent) -- parent-anchored, no tag
    UNRESOLVED = "UNRESOLVED"  # walker could not resolve it


@dataclass(frozen=True)
class ModalSite:
    """One window-family construction found in the tree."""

    relpath: str
    lineno: int
    constructor: str  # e.g. "window", "popup"
    modality: Modality
    tag: str | None
    tag_source: TagSource
    qualname: str  # full enclosing Class/def chain
    owner_qualname: str  # chain truncated at the OUTERMOST enclosing def
    owner_uses_seam: bool  # owner's body mentions _defer_modal_swap

    @property
    def key(self) -> str:
        """Line-number-independent identity: file, owning function, tag.

        Deliberately excludes ``lineno``. Three of the files holding these
        sites move by tens of lines in a single working session; a registry
        keyed on position would be noise. Two constructions that share a key
        (one function building the same tag on two branches) are counted, not
        collapsed -- see ``sites`` in the registry.
        """

        return f"{self.relpath}::{self.owner_qualname}::{self.tag or '<untagged>'}"


@dataclass(frozen=True)
class DiscoveryHole:
    """Something the walker could not analyse -- reported, never swallowed."""

    relpath: str
    lineno: int
    kind: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.relpath}::{self.kind}::{self.detail}"


@dataclass
class _ModuleFacts:
    """Per-file facts the site walker needs while it walks."""

    dpg_aliases: set[str] = field(default_factory=set)
    module_consts: dict[str, str] = field(default_factory=dict)
    module_aliases: dict[str, str] = field(default_factory=dict)


def _module_name_for(relpath: str) -> str:
    return relpath[: -len(".py")].replace("/", ".")


def iter_source_files(root: Path = PACKAGE_ROOT) -> list[Path]:
    """Every ``.py`` under ``root``, ``__pycache__`` excluded, sorted."""

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return sorted(found)


def _relpath(path: Path, base: Path | None = None) -> str:
    """Repo-relative POSIX path.

    ``base`` exists so the walker can be pointed at a copy of the tree that
    carries one deliberate mutation: the gate's own self-check runs the real
    discovery and the real assertions against such a copy, which is the only
    honest way to show that a gate can actually fail.
    """

    return path.relative_to(base or REPO_ROOT).as_posix()


def _collect_module_facts(tree: ast.Module, relpath: str) -> tuple[_ModuleFacts, list[DiscoveryHole]]:
    """Aliases for the Dear PyGui module, module constants, module aliases."""

    facts = _ModuleFacts()
    holes: list[DiscoveryHole] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _KNOWN_DPG_MODULE:
                    facts.dpg_aliases.add(alias.asname or alias.name)
                elif alias.name.startswith("dearpygui"):
                    holes.append(
                        DiscoveryHole(
                            relpath,
                            node.lineno,
                            "unknown_dpg_import",
                            f"import {alias.name} as {alias.asname}",
                        )
                    )
                else:
                    facts.module_aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base.startswith("dearpygui"):
                for alias in node.names:
                    if alias.name in WINDOW_FAMILY:
                        holes.append(
                            DiscoveryHole(
                                relpath,
                                node.lineno,
                                "bare_constructor_import",
                                f"from {base} import {alias.name}",
                            )
                        )
                    elif alias.name == "dearpygui":
                        facts.dpg_aliases.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    facts.module_aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        facts.module_consts[target.id] = value.value

    return facts, holes


def _const_tables(files: list[Path], base: Path | None = None) -> dict[str, dict[str, str]]:
    """``module dotted name -> {CONST: "value"}`` for cross-module tag lookups."""

    table: dict[str, dict[str, str]] = {}
    for path in files:
        relpath = _relpath(path, base)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        facts, _ = _collect_module_facts(tree, relpath)
        table[_module_name_for(relpath)] = facts.module_consts
    return table


class _SiteVisitor(ast.NodeVisitor):
    """Walks one module, recording window-family calls and discovery holes."""

    def __init__(
        self,
        relpath: str,
        facts: _ModuleFacts,
        const_tables: dict[str, dict[str, str]],
    ) -> None:
        self.relpath = relpath
        self.facts = facts
        self.const_tables = const_tables
        self.sites: list[ModalSite] = []
        self.holes: list[DiscoveryHole] = []
        # Stack of (name, node) for enclosing classes/functions.
        self._scope: list[tuple[str, ast.AST]] = []
        # Innermost function's parameter defaults that are resolvable strings.
        self._param_defaults: list[dict[str, str]] = [{}]
        # Locally-assigned dict literals, for the ``**kwargs`` splat form.
        self._local_dicts: list[dict[str, ast.Dict]] = [{}]

    # -- scope bookkeeping ---------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append((node.name, node))
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node) -> None:
        self._scope.append((node.name, node))
        self._param_defaults.append(_resolvable_param_defaults(node, self.facts, self.const_tables))
        self._local_dicts.append(_local_dict_literals(node))
        self.generic_visit(node)
        self._local_dicts.pop()
        self._param_defaults.pop()
        self._scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # -- the actual search ---------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        self._check_dynamic_dispatch(node)
        constructor = self._constructor_name(node)
        if constructor is not None:
            self.sites.append(self._build_site(node, constructor))
        self.generic_visit(node)

    def _constructor_name(self, node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in WINDOW_FAMILY:
            value = func.value
            if isinstance(value, ast.Name) and value.id in self.facts.dpg_aliases:
                return func.attr
        return None

    def _check_dynamic_dispatch(self, node: ast.Call) -> None:
        """``getattr(dpg, name)`` / ``setattr(dpg, ...)`` defeat static reading.

        There are only a couple of these in the tree and neither builds a
        window, but the walker cannot know that -- so it reports them and the
        registry has to acknowledge each one by name.
        """

        func = node.func
        if not isinstance(func, ast.Name) or func.id not in {"getattr", "setattr"}:
            return
        if not node.args:
            return
        target = node.args[0]
        target_name = None
        if isinstance(target, ast.Name):
            target_name = target.id
        if target_name is None:
            return
        looks_like_dpg = target_name in self.facts.dpg_aliases or "dpg" in target_name.lower()
        if not looks_like_dpg:
            return
        self.holes.append(
            DiscoveryHole(
                self.relpath,
                node.lineno,
                "dynamic_dpg_dispatch",
                f"{func.id}({target_name}, ...) in {self._qualname()}",
            )
        )

    # -- site construction ---------------------------------------------------

    def _qualname(self) -> str:
        return ".".join(name for name, _ in self._scope) or "<module>"

    def _owner_qualname(self) -> str:
        """Scope chain truncated after the OUTERMOST enclosing function.

        Sites are almost always built inside a nested ``_open()`` closure that
        the seam call receives as ``open_fn``. Classifying by the closure name
        would key the registry on an implementation detail; the meaningful unit
        is the top-level function or method a caller invokes.
        """

        parts: list[str] = []
        for name, node in self._scope:
            parts.append(name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
        return ".".join(parts) or "<module>"

    def _owner_node(self):
        for _, node in self._scope:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def _owner_uses_seam(self) -> bool:
        owner = self._owner_node()
        if owner is None:
            return False
        return _mentions_seam(owner)

    def _build_site(self, node: ast.Call, constructor: str) -> ModalSite:
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        splats = [kw.value for kw in node.keywords if kw.arg is None]

        splat_dict: dict[str, ast.expr] | None = None
        splat_unresolved = False
        for splat in splats:
            resolved = self._resolve_splat(splat)
            if resolved is None:
                splat_unresolved = True
            else:
                splat_dict = {**(splat_dict or {}), **resolved}

        merged: dict[str, ast.expr] = dict(splat_dict or {})
        merged.update(kwargs)

        modality = self._modality(constructor, merged, splat_unresolved)
        tag, tag_source = self._resolve_tag(node, constructor, merged, splat_dict, splat_unresolved)

        return ModalSite(
            relpath=self.relpath,
            lineno=node.lineno,
            constructor=constructor,
            modality=modality,
            tag=tag,
            tag_source=tag_source,
            qualname=self._qualname(),
            owner_qualname=self._owner_qualname(),
            owner_uses_seam=self._owner_uses_seam(),
        )

    def _resolve_splat(self, splat: ast.expr) -> dict[str, ast.expr] | None:
        """Resolve ``**name`` to a dict literal built in the same function."""

        if isinstance(splat, ast.Dict):
            return _dict_literal_items(splat)
        if isinstance(splat, ast.Name):
            local = self._local_dicts[-1].get(splat.id)
            if local is not None:
                return _dict_literal_items(local)
        return None

    def _modality(
        self,
        constructor: str,
        merged: dict[str, ast.expr],
        splat_unresolved: bool,
    ) -> Modality:
        if constructor in POPUP_CONSTRUCTORS:
            return Modality.POPUP
        modal = merged.get("modal")
        if modal is None:
            # A window with no ``modal`` kwarg is non-modal -- unless an
            # unresolved splat could be carrying one.
            return Modality.UNDECIDABLE if splat_unresolved else Modality.NON_MODAL
        if isinstance(modal, ast.Constant) and isinstance(modal.value, bool):
            return Modality.MODAL if modal.value else Modality.NON_MODAL
        return Modality.UNDECIDABLE

    def _resolve_tag(
        self,
        node: ast.Call,
        constructor: str,
        merged: dict[str, ast.expr],
        splat_dict: dict[str, ast.expr] | None,
        splat_unresolved: bool,
    ) -> tuple[str | None, TagSource]:
        tag_node = merged.get("tag")
        if tag_node is None:
            tag_node = merged.get("id")  # DPG's legacy spelling
        if tag_node is None:
            if constructor in POPUP_CONSTRUCTORS and node.args:
                # dpg.popup(parent) -- anchored to a widget, no stable tag.
                return None, TagSource.POSITIONAL
            return None, TagSource.UNRESOLVED

        came_from_splat = splat_dict is not None and merged.get("tag") is splat_dict.get("tag")
        value = self._resolve_string(tag_node)
        if value is None:
            return None, TagSource.UNRESOLVED
        if came_from_splat:
            return value, TagSource.KWARGS_DICT
        if isinstance(tag_node, ast.Constant):
            return value, TagSource.LITERAL
        if isinstance(tag_node, ast.Attribute):
            return value, TagSource.IMPORTED_CONST
        if isinstance(tag_node, ast.Name):
            if tag_node.id in self._param_defaults[-1]:
                return value, TagSource.PARAM_DEFAULT
            return value, TagSource.MODULE_CONST
        return value, TagSource.UNRESOLVED

    def _resolve_string(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Constant):
            return expr.value if isinstance(expr.value, str) else None
        if isinstance(expr, ast.Name):
            for scope in reversed(self._param_defaults):
                if expr.id in scope:
                    return scope[expr.id]
            return self.facts.module_consts.get(expr.id)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            module = self.facts.module_aliases.get(expr.value.id)
            if module is None:
                return None
            return self.const_tables.get(module, {}).get(expr.attr)
        return None


def _dict_literal_items(node: ast.Dict) -> dict[str, ast.expr]:
    items: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            items[key.value] = value
    return items


def _local_dict_literals(func) -> dict[str, ast.Dict]:
    """``name -> dict literal`` for ``name = {...}`` directly in ``func``.

    Only the literal assignment is tracked. Later ``name["k"] = v`` mutations
    are NOT followed -- a site whose tag arrives that way resolves to whatever
    the literal held, which is why the registry pins ``tag_source`` too: if the
    shape changes, the recorded source stops matching and the gate fires.
    """

    found: dict[str, ast.Dict] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def _resolvable_param_defaults(
    func,
    facts: _ModuleFacts,
    const_tables: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Parameter names whose default is a resolvable string constant."""

    args = func.args
    resolved: dict[str, str] = {}
    positional = args.posonlyargs + args.args
    for arg, default in zip(positional[len(positional) - len(args.defaults) :], args.defaults):
        value = _static_string(default, facts, const_tables)
        if value is not None:
            resolved[arg.arg] = value
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is None:
            continue
        value = _static_string(default, facts, const_tables)
        if value is not None:
            resolved[arg.arg] = value
    return resolved


def _static_string(
    expr: ast.expr,
    facts: _ModuleFacts,
    const_tables: dict[str, dict[str, str]],
) -> str | None:
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else None
    if isinstance(expr, ast.Name):
        return facts.module_consts.get(expr.id)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        module = facts.module_aliases.get(expr.value.id)
        if module is None:
            return None
        return const_tables.get(module, {}).get(expr.attr)
    return None


def discover(
    root: Path = PACKAGE_ROOT, base: Path | None = None
) -> tuple[list[ModalSite], list[DiscoveryHole]]:
    """Every window-family construction under ``root``, plus discovery holes."""

    files = iter_source_files(root)
    tables = _const_tables(files, base)

    sites: list[ModalSite] = []
    holes: list[DiscoveryHole] = []
    for path in files:
        relpath = _relpath(path, base)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        facts, import_holes = _collect_module_facts(tree, relpath)
        holes.extend(import_holes)
        if not facts.dpg_aliases:
            # No Dear PyGui in this module: it cannot construct a window. The
            # unknown-import hole above is what catches an exotic import form.
            continue
        visitor = _SiteVisitor(relpath, facts, tables)
        visitor.visit(tree)
        sites.extend(visitor.sites)
        holes.extend(visitor.holes)

    sites.sort(key=lambda s: (s.relpath, s.lineno))
    holes.sort(key=lambda h: (h.relpath, h.lineno))
    return sites, holes


def function_uses_seam(relpath: str, qualname: str, root: Path = REPO_ROOT) -> bool | None:
    """Does ``qualname`` in ``relpath`` mention the seam? ``None`` if not found.

    Used to check registry entries that claim a *caller* does the seaming.
    """

    path = root / relpath
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = _find_qualname(tree, qualname)
    if target is None:
        return None
    return _mentions_seam(target)


def _mentions_seam(node) -> bool:
    """Does ``node``'s subtree actually CALL the seam with a modal builder?

    A *mention* proves nothing, and this used to accept one. Any constant equal
    to ``"_defer_modal_swap"`` satisfied the old check, so a dead
    ``getattr(shell, "_defer_modal_swap", None)`` parked next to an inline
    ``delete_item`` + ``dpg.window`` pair classified as SEAM -- the exact
    same-pass teardown+create shape the registry exists to flag. The gate's own
    self-check deleted the mention outright, so it never exercised that
    mutation.

    Requiring a real call was still not enough, because the alias bookkeeping
    was flow-insensitive: it collected every name ever bound to a seam lookup
    anywhere in the function and then accepted a call through that name from
    anywhere else. So both of these classified as SEAM while ``_open`` ran
    inline::

        modal_swap = getattr(shell, "_defer_modal_swap", None)
        modal_swap = lambda fn, **kwargs: fn()
        modal_swap(_open)

        if False:
            modal_swap = getattr(shell, "_defer_modal_swap", None)
        modal_swap = lambda fn, **kwargs: fn()
        modal_swap(_open)

    The call's *reaching definition* must therefore be the seam lookup itself.
    A name counts at a call only when a seam lookup reaches it on every path
    that gets there, and never when the function rebinds that name to anything
    else -- a superseded binding is not a seam.

    Two call forms count, because the codebase genuinely uses both:

    * ``self._defer_modal_swap(open_fn, ...)`` -- an attribute call, the shape
      every hop inside ``AppShell`` uses.
    * ``modal_swap = getattr(shell, "_defer_modal_swap", None)`` followed by
      ``modal_swap(_open, ...)``. This one is load-bearing, not defensive:
      eight screen-module hops reach the seam this way so the mocked suite can
      hand screens a lightweight fake shell that carries no seam. Requiring a
      bare attribute call would report all eight as inline.

    Either way the call must supply the modal-building closure -- first
    positional argument or ``open_fn=`` -- since a seam call with no builder
    cannot be what opens the modal.
    """

    body = getattr(node, "body", None)
    if not isinstance(body, list):
        return False
    return _block_calls_seam(body, frozenset(), _rebound_to_non_seam(node))[0]


def _rebound_to_non_seam(node) -> frozenset[str]:
    """Names the subtree ever binds to something that is not a seam lookup.

    Flow analysis alone would still accept a rebind the walk cannot order --
    inside a nested closure, a loop that runs later, a comprehension. Any name
    this function reassigns to a non-seam value is therefore refused outright,
    wherever the reassignment sits. Refusing too much reports a real seam hop
    as inline, which fails the gate loudly; accepting too much lets an inline
    teardown-then-create pass as cured, which is silent.
    """

    poisoned: set[str] = set()

    def bind(target, value) -> None:
        if isinstance(target, ast.Name):
            if value is None or not _is_seam_lookup(value):
                poisoned.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                bind(element, None)
        elif isinstance(target, ast.Starred):
            bind(target.value, None)

    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                bind(target, sub.value)
        elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)):
            bind(sub.target, sub.value)
        elif isinstance(sub, ast.AugAssign):
            bind(sub.target, None)
        elif isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(sub.target, None)
        elif isinstance(sub, ast.withitem) and sub.optional_vars is not None:
            bind(sub.optional_vars, None)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            poisoned.add(sub.name)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            poisoned.add(sub.name)
        elif isinstance(sub, ast.arg):
            poisoned.add(sub.arg)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                poisoned.add((alias.asname or alias.name).split(".")[0])
    return frozenset(poisoned)


def _block_calls_seam(
    statements: list, state: frozenset[str], poisoned: frozenset[str]
) -> tuple[bool, frozenset[str]]:
    """Walk a statement list in order, carrying the seam bindings that reach it."""

    found = False
    for statement in statements:
        hit, state = _statement_calls_seam(statement, state, poisoned)
        found = found or hit
    return found, state


def _statement_calls_seam(
    statement, state: frozenset[str], poisoned: frozenset[str]
) -> tuple[bool, frozenset[str]]:
    if isinstance(statement, ast.If):
        found = _calls_seam_in(statement.test, state, poisoned)
        taken = (
            bool(statement.test.value)
            if isinstance(statement.test, ast.Constant)
            else None
        )
        # ``if False:`` never runs, so a lookup parked inside it binds nothing.
        if taken is True:
            hit, state = _block_calls_seam(statement.body, state, poisoned)
            return found or hit, state
        if taken is False:
            hit, state = _block_calls_seam(statement.orelse, state, poisoned)
            return found or hit, state
        taken_hit, taken_state = _block_calls_seam(statement.body, state, poisoned)
        else_hit, else_state = _block_calls_seam(statement.orelse, state, poisoned)
        return found or taken_hit or else_hit, taken_state & else_state

    if isinstance(statement, ast.Try):
        found, body_state = _block_calls_seam(statement.body, state, poisoned)
        hit, body_state = _block_calls_seam(statement.orelse, body_state, poisoned)
        found = found or hit
        # A handler can be entered from anywhere in the body, so it sees the
        # bindings that were live on the way IN, not the ones the body left.
        outs = [body_state]
        for handler in statement.handlers:
            hit, handler_state = _block_calls_seam(handler.body, state, poisoned)
            found = found or hit
            outs.append(handler_state)
        joined = frozenset.intersection(*outs) if outs else state
        hit, joined = _block_calls_seam(statement.finalbody, joined, poisoned)
        return found or hit, joined

    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        header = statement.iter if hasattr(statement, "iter") else statement.test
        found = _calls_seam_in(header, state, poisoned)
        # The body may run zero times, so nothing it binds is guaranteed after.
        hit, _body_state = _block_calls_seam(statement.body, state, poisoned)
        found = found or hit
        hit, _else_state = _block_calls_seam(statement.orelse, state, poisoned)
        return found or hit, state

    if isinstance(statement, (ast.With, ast.AsyncWith)):
        found = any(
            _calls_seam_in(item.context_expr, state, poisoned)
            for item in statement.items
        )
        hit, state = _block_calls_seam(statement.body, state, poisoned)
        return found or hit, state

    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # A nested definition runs later, capturing what is bound here. Its own
        # bindings never reach the enclosing block, so its out-state is dropped.
        hit, _inner = _block_calls_seam(statement.body, state, poisoned)
        return hit, state

    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        value = getattr(statement, "value", None)
        found = value is not None and _calls_seam_in(value, state, poisoned)
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        seam_value = value is not None and _is_seam_lookup(value)
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            state = state | {target.id} if seam_value else state - {target.id}
        return found, state

    return _calls_seam_in(statement, state, poisoned), state


def _calls_seam_in(node, state: frozenset[str], poisoned: frozenset[str]) -> bool:
    """Any seam call with a builder inside ``node``, given the reaching bindings."""

    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute):
            called_seam = func.attr == SEAM_ATTR
        elif isinstance(func, ast.Name):
            called_seam = func.id == SEAM_ATTR or (
                func.id in state and func.id not in poisoned
            )
        else:
            called_seam = False
        if called_seam and _supplies_open_fn(sub):
            return True
    return False


def _is_seam_lookup(value: ast.expr) -> bool:
    """Is ``value`` an expression that yields the seam function itself?"""

    if isinstance(value, ast.Attribute) and value.attr == SEAM_ATTR:
        return True
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "getattr"
        and len(value.args) >= 2
        and isinstance(value.args[1], ast.Constant)
        and value.args[1].value == SEAM_ATTR
    ):
        return True
    return False


def _supplies_open_fn(call: ast.Call) -> bool:
    """Does ``call`` pass something as the seam's ``open_fn``?

    A literal (``None``, a string) does not count: the seam calls this
    argument, so a constant there is not a modal builder.
    """

    candidate: ast.expr | None = None
    if call.args:
        candidate = call.args[0]
    else:
        for keyword in call.keywords:
            if keyword.arg == "open_fn":
                candidate = keyword.value
                break
    if candidate is None:
        return False
    return not isinstance(candidate, ast.Constant)


def _find_qualname(tree: ast.Module, qualname: str):
    parts = qualname.split(".")
    scope: list = list(tree.body)
    node = None
    for part in parts:
        node = None
        for candidate in scope:
            if isinstance(
                candidate, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ) and candidate.name == part:
                node = candidate
                break
        if node is None:
            return None
        scope = list(node.body)
    return node


def read_string_tuple(relpath: str, const_name: str, root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Read a module-level tuple/list of strings statically.

    Used for ``app_shell.CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS``, whose
    members mix bare literals, local constants and ``other_module.CONST``
    attributes. Reading it by AST rather than by import keeps the whole gate
    free of Dear PyGui and of any import side effect. Raises if a member cannot
    be resolved -- an unresolvable member is exactly the kind of drift the gate
    exists to surface.
    """

    path = root / relpath
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    facts, _ = _collect_module_facts(tree, relpath)
    tables = _const_tables(iter_source_files())

    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == const_name for t in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            raise ValueError(f"{relpath}::{const_name} is not a tuple/list/set literal")
        out: list[str] = []
        for element in value.elts:
            resolved = _static_string(element, facts, tables)
            if resolved is None:
                raise ValueError(
                    f"{relpath}::{const_name} member at line "
                    f"{getattr(element, 'lineno', '?')} does not resolve to a string"
                )
            out.append(resolved)
        return tuple(out)

    raise ValueError(f"{relpath} has no module-level {const_name}")


def find_callers(
    relpath: str,
    function_name: str,
    root: Path = PACKAGE_ROOT,
    base: Path | None = None,
) -> list[str]:
    """``relpath::qualname`` of every function in ``root`` calling ``function_name``.

    Attribute and bare-name calls both count; this is a name match, not a
    resolved-symbol match. It sees CALLS only: a bound method handed over as a
    callback (``callback=shell.fn``), a ``getattr`` string or an aliased import
    is not counted, so an empty result is not proof that nothing reaches it.
    """

    hits: list[str] = []
    for path in iter_source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits.extend(_calls_in_module(tree, _relpath(path, base), function_name))
    return sorted(set(hits))


def _calls_in_module(tree: ast.Module, relpath: str, function_name: str) -> list[str]:
    hits: list[str] = []

    def walk(node, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, scope + [child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = None
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                if name == function_name:
                    hits.append(f"{relpath}::{'.'.join(scope) or '<module>'}")
            walk(child, scope)

    walk(tree, [])
    return hits
