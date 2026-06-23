"""Shared filesystem-path scrubber for user-shareable artifacts.

Several artifacts the wrapper produces are explicitly shared with other people
— the diagnostic bundle, the on-screen diagnostics event log, standalone
Health Report exports, crash reports, the rotating app log (SUPPORT.md asks
users to attach it), and wear-ledger service notes. None of these may carry
the operator's Windows home path, because the home path contains the account
*username* — which, on a display-name account, is the person's real name
(``C:\\Users\\Jane Doe\\...``).

Earlier each module rolled its own path regex of the shape
``[A-Za-z]:[\\/][^\\s...]+`` — a class that stops at the **first whitespace**.
That truncates ``C:\\Users\\Jane Doe\\...`` at the space, leaving ``Jane`` (the
first name) leaking verbatim into the "scrubbed" output. This module is the
single shared scrubber those call sites now delegate to, so the two can never
drift apart again.

Design
------
A username is exactly the *one path component* directly after a
``Users``/``home`` root, and a path component is bounded by **separators**, not
by spaces — so a display-name username with interior spaces ("Jane Doe", or
even "Mary Jane Watson") is still a single component. We therefore:

1. Extract path-shaped tokens from free text. A token starts at a recognizable
   path anchor (drive letter, UNC ``\\\\``, POSIX ``/Users/`` / ``/home/``,
   ``~/``, or a ``%USERPROFILE%`` / ``$LOCALAPPDATA``-style env var) and runs
   up to a **real terminator** — a double quote, angle bracket, pipe, tab, or
   line boundary (the chars that are *illegal* inside a Windows path) — or to
   the whitespace gap before the *next* path anchor on the same line, so a line
   carrying two paths reduces each independently (a whitespace-prose connector
   like "copy A to B" can't merge them into one token whose tail leaks the
   second path's username; a *non*-whitespace connector — a repr-list ``', '``,
   comma, semicolon, ``) (``, or zero-separator glue — is split reducer-side
   instead, see below). Neither a lone interior space nor a username-legal
   punctuation char (``'  (  )  {  }  [  ]``) ends the token, so a display-name
   username — spaced ("Jane Doe"), apostrophed ("O'Connor"), or bracketed
   ("Jane (Admin)", "Jane[Work]") — survives into the reducer intact (where the
   whole component is dropped) instead of being split mid-name and leaking the
   trailing fragment.
2. Reduce each token by splitting on separators (robust to interior spaces and
   to JSON-escaped ``\\\\``). The reduction tries these in order; the FIRST that
   applies wins:

   - contains the app-data marker (``ZDUltimateLegend``) → ``<APP_DATA>/<tail>``
   - contains an app source marker (``zd_app`` / ``_internal``) → kept from that
     marker on (so crash-traceback frames stay diagnostically useful) with the
     home/username prefix dropped
   - rooted at an **env-var / tilde** home anchor (``%USERPROFILE%`` /
     ``$env:APPDATA`` / ``~``) with no marker → a bare placeholder, the tail
     **dropped** (``<APP_DATA>`` for ``APPDATA``/``LOCALAPPDATA``, else
     ``<HOME>``). An env home var expands to a directory that *contains the
     username*, so its tail is an arbitrary user path we cannot structurally
     vet — and a glued second path's username could otherwise ride out as its
     leaf basename. Ordered *after* the marker checks so a real data path like
     ``%LOCALAPPDATA%\\ZDUltimateLegend\\logs\\app.log`` still keeps its tail.
   - rooted at ``Users``/``home`` → the username component is dropped and the
     path is reduced to its basename (or ``<HOME>`` if it *was* the bare home)
   - otherwise (a non-home drive/UNC path) → basename

Second-path closure (structural, width-free)
--------------------------------------------
Two (or more) paths can also be merged into one token by a *non-whitespace*
connector the body tempering cannot see — a ``repr([...])`` list separator
(``', '``), a bare comma/semicolon, ``) (``, or no separator at all (a second
drive letter glued straight onto the first path's basename). Left unhandled,
steps 1/2 above would re-emit the merged tail verbatim and leak the *second*
path's username. Two mechanisms close this, both independent of any separator
*width* arithmetic:

(a) The reducer walks the token left-to-right and splits at each fresh *drive*
    or *env-var* anchor (:data:`_SECOND_PATH_ANCHOR_RE`), reducing every segment
    independently — an iteration, so a token glued from arbitrarily many paths
    uses O(1) Python stack (see :func:`_reduce_path_token`).

(b) The emitted tail (the components after the app-data / app-source marker, or
    after a home root's username) is trimmed at the FIRST component that
    re-roots a second path, detected **structurally** — with NO comparison to
    the path's own separator width. A tail component re-roots when ANY holds
    (see :func:`_tail_before_reanchor` / :func:`_is_reroot_run`):

      * it lower-cases to a ``Users``/``home`` root (a drive-less or POSIX home
        second, ``...\\a.json\\Users\\Bob`` / ``.../Users/Bob``);
      * its preceding separator run is **two or more characters** (a native
        ``\\\\`` UNC root, a JSON-escaped ``\\\\``, a triple/quad-escaped repr
        run, or a glued ``//`` UNC root) — a single path uses exactly *one*
        separator between components, so any wider run is either a re-root or an
        un-vettable doubled join;
      * its preceding separator run is **two or more forward slashes** (``//`` —
        a forward-UNC root, and a sloppy ``logs//sub`` join);
      * the component itself is an **env-var / tilde root** (``%APPDATA%`` /
        ``$env:USERPROFILE`` / ``~``) — a separator-introduced env re-root the
        drive/env splitter (a) deliberately leaves for the current path.

   So a second path's username can never ride along on the first path's reduced
   tail, whatever the connector (or absence of one).

Why width-free
--------------
An earlier design compared a tail separator run to the path's own *interior*
(non-leading) separator width — keeping a uniformly-doubled (JSON-escaped /
``%r``-repr) single path's multi-component tail intact while still catching a
glued UNC re-root that was *wider* than that interior. A full adversarial matrix
proved that comparison has an **irreducible collision**: a uniformly-doubled
path's interior runs are themselves width 2, *identical* to a glued native-UNC
``\\\\`` root (width 2). No width threshold can keep the first (interior 2) yet
drop the second (root 2). The structural rule in (b) removes the comparison
entirely and treats *any* multi-character run as a re-root.

Accepted tail loss
------------------
The cost of width-freedom is borne by uniformly-doubled paths only: a
JSON-escaped / ``%r``-repr path whose every separator is width 2+ has its tail
trimmed at the first component, so it collapses to ``<APP_DATA>`` / ``<HOME>`` /
the bare source marker rather than retaining the full tail; a sloppy
``logs//sub`` join trims to ``<APP_DATA>/logs``. This is a diagnostic nicety
lost, never a username leaked — the security contract (no account username in a
shared artifact) is absolute and best-effort tail fidelity yields to it.
Single-separator paths, the overwhelmingly common form, keep their full tail.

Canonicalization siblings (interior ``.`` and ``..``)
-----------------------------------------------------
Windows resolves ``.`` (current-dir) and ``..`` (parent) components *before* it
opens a path, so a decorated home spelling still reaches the REAL home and the
component the OS lands on is still the account username:
``C:\\Users\\.\\Jane Doe`` IS ``C:\\Users\\Jane Doe`` and
``C:\\Users\\Alice\\..\\Bob`` IS ``C:\\Users\\Bob`` (both verified ``isdir`` on a
live box). A naive split-on-separators reducer leaked both — it dropped the
literal post-root component (the ``.``, or the pre-``..`` name) as the
"username" and re-emitted the *resolved* username as the basename. The
home-rooted reduction (step 4) therefore canonicalizes:

* ``.`` (and empty) components are no-ops — skipped both when locating the
  username after the root (so ``Users\\.\\<name>`` drops ``<name>``) and when
  taking the tail basename (so a legitimate ``.\\file.log`` still keeps
  ``file.log``, only the username dropped).
* a ``..`` *anywhere after* the home root collapses the whole reduction to
  ``<HOME>`` — the safe floor. A ``..`` can reposition the username to the leaf
  and cannot be statically resolved without a syscall, so we never try. (A
  ``..`` *before* the root is harmless — the root is still found and its next
  component dropped.) The same ``..`` trim guards :func:`_tail_before_reanchor`,
  so a ``..`` walking back up out of an app-data / app-source tail to a username
  leaf (``...\\ZDUltimateLegend\\..\\..\\Eve Ng``) is trimmed off there too.

Both use the trailing-dot-tolerant :func:`_is_home_root`, so decorated +
traversal combos (``C:\\Users.\\.\\Jane Doe``) canonicalize as well.

Accepted residual (non-resolving spellings)
-------------------------------------------
Three *other* component spellings can name a real directory without matching the
literal ``Users``/``home`` root word, so they may pass a basename through — but
none is a real *account* path, and the wrapper's own shared artifacts carry only
OS-normalized paths, so the operator's true username is always dropped. No
special behavior is applied for these (doc-only):

* an **8.3 short name** with no long alias (``...\\JANEDO~1\\...``) — a
  filesystem alias we cannot expand without an alias-table syscall;
* an **alternate-data-stream** component (``...\\Jane Doe:stream``) — the
  ``:stream`` suffix is an NTFS ADS, not a separator;
* a **leading-space root** (``C:\\ Users\\...``) — Windows coalesces a
  component's *trailing* dots/spaces (handled by :func:`_is_home_root`) but NOT
  leading spaces, so `` Users`` is a genuinely different directory.

The reducer never emits the dropped username, and non-path text is returned
unchanged (a plain controller/profile name like "ZD Ultimate Legend" is not a
path, so it is untouched).
"""

from __future__ import annotations

import logging
import re


__all__ = [
    "APP_DATA_PLACEHOLDER",
    "HOME_PLACEHOLDER",
    "PathScrubbingFormatter",
    "scrub_paths",
    "scrub_value",
]


# Placeholder the wrapper's own data directory collapses to.
APP_DATA_PLACEHOLDER = "<APP_DATA>"
# Placeholder a bare user-home directory (no file tail) collapses to.
HOME_PLACEHOLDER = "<HOME>"

# Path component whose presence marks the wrapper's user-data root. Matches
# ``zd_app.version.__app_id__``; kept as a literal so this module stays free of
# import cycles and usable from ``main_zd``'s logging filter at startup.
_APP_DIR_NAME = "ZDUltimateLegend"

# Components that mark the start of the app's own source tree. A path that
# contains one is reduced to *that component onward* rather than to a bare
# basename, so a crash traceback frame stays as ``zd_app/services/foo.py``
# instead of just ``foo.py``. The home/username prefix before it is still
# dropped. ``_internal`` covers PyInstaller-frozen frame paths.
_SOURCE_MARKERS = ("zd_app", "_internal")

# Roots whose immediately-following component is the account username.
_HOME_ROOTS = ("users", "home")

# Path-shaped-token matcher, assembled from parts (no VERBOSE, to keep the
# character classes unambiguous). The leading look-behind keeps ``https://``
# (and other ``scheme://`` text) from being mistaken for a ``s:/`` drive path.
#
# Anchors — the recognizable ways a path can start:
#   [A-Za-z]:[\\/]        drive root      C:\ or C:/
#   \\                    UNC root        \\host\share\...
#   (?<!:)//(?=host)      forward UNC     //host/share\...  (NOT a scheme's //)
#   /(?:Users|home)[. ]*/      POSIX home      /Users/  /home/  (trailing . / space ok)
#   [\\/](?:Users|home)[. ]*[\\/]  rooted home  \Users\  \home\  (either slash; trailing . / space ok)
#   ~[\\/]                tilde home      ~/  ~\
#   %VAR%[\\/]            env-var home    %USERPROFILE%\ ...
#   $VAR / $env:VAR / ${VAR}  shell env-var home  $LOCALAPPDATA\ ...
#
# The forward-slash UNC branch (``//host/share/...``) is the slash-flipped twin
# of the ``\\`` UNC root; without it a standalone ``//fileserver/share/Jane Doe``
# matched no anchor and leaked the username verbatim. It is guarded two ways so
# it cannot swallow a URL: the shared leading look-behind already blocks a
# ``scheme:`` prefix (``https`` is alphanumeric, so ``//`` after ``https:`` is
# preceded by ``s``… no — by ``:``), and the branch-local ``(?<!:)`` rejects the
# ``scheme://`` colon specifically (``http://``, ``file://``, ``smb://`` all have
# ``:`` immediately before ``//``). The ``(?=[^/\\\s])`` requires a real host
# char next, so a bare ``//`` / ``///`` / ``// `` never anchors. (``\\?\`` and
# ``\\?\UNC\`` extended-length prefixes need no new branch: the ``\\`` root
# already matches them and the reducer normalizes the prefix away — see
# :data:`_EXT_LEN_PREFIX_RE`.)
#
# The rooted-home branch catches a drive-LESS Windows home path that starts at
# a bare separator — ``\Users\Jane Doe\...`` (also what ``%HOMEPATH%`` expands
# to) or ``\home\...`` — which the drive-letter and POSIX branches both miss,
# leaving the username leaking verbatim. It accepts either slash direction, so
# it subsumes the POSIX ``/Users/`` / ``/home/`` branch above (kept for its
# documented intent); the redundant overlap is harmless since both alternatives
# match the identical span. The reducer treats the leading empty component (from
# the bare separator) as a no-op, then locates the ``Users``/``home`` root and
# drops the NEXT component (the username) exactly as for a drive-letter path.
#
# Trailing dots/spaces on the root word (``\Users.\`` / ``/Users /`` /
# ``\home...\``) are tolerated via ``[. ]*`` before the closing separator.
# Windows silently coalesces a path component's trailing dots and spaces, so
# ``\Users.\<name>`` IS the real home — but without ``[. ]*`` the drive-less /
# POSIX anchor (which needs ``Users``/``home`` *immediately* before its
# separator) never matched it, and the whole path passed through verbatim,
# leaking the username. (A *drive-rooted* ``C:\Users.\<name>`` still anchors via
# its drive letter; its decorated root word is normalized reducer-side instead
# — see :func:`_is_home_root`, the shared coalescing accessor for both paths.)
# The ``[. ]*`` is bounded and sits after the fixed ``Users``/``home`` literal,
# so it adds no catastrophic-backtracking risk to the body's anchor lookahead.
#
# The ``$``-env-var branch matches the shell/PowerShell spellings of the home
# env vars (``$LOCALAPPDATA\``, ``$env:USERPROFILE\``, ``${APPDATA}\``) the same
# way the ``%VAR%`` branch matches the cmd spelling — defensive ("all path
# shapes"). The reducer treats ``$LOCALAPPDATA`` / ``$env:USERPROFILE`` as an
# env-var root: an app-data marker still collapses to ``<APP_DATA>``, otherwise
# the env-rooted path drops its (un-vettable) tail to a placeholder.
_PATH_ANCHOR = (
    r"(?:[A-Za-z]:[\\/]"
    r"|\\\\"
    r"|(?<!:)//(?=[^/\\\s])"
    r"|/(?:Users|home)[. ]*/"
    r"|[\\/](?:Users|home)[. ]*[\\/]"
    r"|~[\\/]"
    r"|%(?:USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)%[\\/]"
    r"|\$(?:env:)?\{?(?:USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)\}?[\\/])"
)
# The body runs up to either a *real* terminator — a double quote, an angle
# bracket, a pipe, a tab, or a line boundary (the chars that are **illegal in a
# Windows path**, `" < > |`, plus control/quote delimiters, so none of them can
# appear inside a genuine account/folder name) — or the start of the *next*
# path on the line (see the tempering note below). A single interior space is
# NOT a terminator, so a spaced username ("Jane Doe") is captured whole.
#
# Critically, the terminator class must NOT include ``'  (  )  {  }  [  ]``:
# those are all *legal* in Windows account/folder names ("O'Connor",
# "Jane (Admin)", "Jane[Work]"). Treating them as terminators truncated the
# username component mid-name, so the surname fragment after the bracket
# ("Connor", "Admin", "Work") leaked verbatim into every shared artifact while
# the leading fragment collapsed to ``<HOME>``. Keeping them inside the token
# lets the reducer drop the whole component as designed.
#
# Multi-path tempering. Because a space is not a terminator, a line carrying
# *two* home paths joined by terminator-free prose ("copy A to B", "A wrote B",
# "A and B") would otherwise be captured as ONE token — and the reducer only
# collapses the FIRST path in a token, so the second path's username leaked
# verbatim (e.g. ``copy <APP_DATA>/p.json to C:/Users/Alice Doe/...``). The body
# is therefore tempered to also stop before a whitespace gap that is immediately
# followed by the next path anchor, so each path becomes its own match and
# reduces independently. This tempering covers only *whitespace*-joined paths;
# paths merged by a non-whitespace connector (a repr-list ``', '``, comma,
# semicolon, ``) (``) or glued with no separator are split reducer-side instead
# (see ``_SECOND_PATH_ANCHOR_RE`` and ``_reduce_path_token``'s multi-path guard).
#
# The gap MUST be whitespace (``[^\S\r\n\t]+`` — exactly the whitespace the body
# could otherwise consume; ``\r\n\t`` are already terminators). Tempering
# against the anchor *unconditionally* (``(?!anchor)``) would instead fragment a
# single path that contains an interior anchor-shaped run — JSON-escaped ``\\``
# separators, a ``%r``-repr path's doubled backslashes, or an interior directory
# literally named ``Users``/``home`` (``C:\data\Users\Bob\...``, which must
# still reduce to its basename, not leak ``Bob``). Requiring a real whitespace
# gap before the next anchor keeps those single paths intact while still
# splitting genuinely separate paths.
_PATH_BODY = r"(?:(?![^\S\r\n\t]+" + _PATH_ANCHOR + r")[^\"<>|\r\n\t])*"
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])" + _PATH_ANCHOR + _PATH_BODY,
    re.IGNORECASE,
)

# Compiled bare anchor — used by the reducer's multi-path guard to find where a
# token's *leading* anchor ends, so the search for a SECOND path starts in the
# remainder (and can never re-match the leading anchor itself).
_PATH_ANCHOR_RE = re.compile(_PATH_ANCHOR, re.IGNORECASE)

# A *second* path's start that the body's whitespace-only tempering cannot split
# off, because the two paths are joined by a NON-whitespace connector — a
# ``repr([...])`` list separator (``', '``), a bare comma or semicolon, ``) (``,
# or no separator at all (the second drive letter glued straight onto the first
# path's basename, ``...a.jsonC:\\Users\\Bob\\...``). The reducer splits the
# token here so each side reduces independently and the second path's username
# can never ride along on steps 1/2's emitted tail.
#
# Scope of the alternatives is deliberate:
#   * Drive root ``[A-Za-z]:`` — a ``:`` is illegal anywhere else in a Windows
#     path, so a drive letter after the leading anchor is *always* a fresh path.
#     The pattern omits the ``_PATH_TOKEN_RE`` look-behind on purpose so a glued
#     ``...nC:\\`` is still caught, and instead excludes a URL scheme's ``//``
#     via ``/(?!/)`` — ``https://`` is ``s:`` + ``//`` and must NOT read as a
#     drive (matches the ``test_url_not_mangled`` intent even when a URL is glued
#     into a token's body).
#   * Env-var anchors — same set the leading anchor accepts; a ``%VAR%`` / ``$VAR``
#     mid-token is likewise a fresh path, BUT only when it is not immediately
#     preceded by a separator. ``%`` and ``$`` are *legal* in Windows account /
#     folder names, so an env-var-shaped run sitting right after a ``\``/``/`` is
#     a path *component* of the current path — e.g. a username (or directory)
#     literally named ``%APPDATA%`` in ``C:\Users\%APPDATA%\Documents\f.txt`` —
#     not a fresh second path. Splitting there mangled the single path into a
#     stray ``Users`` + basename (``Usersf.txt``; no username leak, but wrong
#     output). The ``(?<![\\/])`` look-behind suppresses that: a genuine second
#     path's env-var is introduced by a connector (``,`` ``;`` ``'`` ``(``) or a
#     basename char — never a separator — so the real multi-path split still
#     fires. (The drive alternative needs no such guard: a ``:`` is illegal
#     mid-name, so a drive letter is *always* a fresh path, and the glued-drive
#     case must keep splitting.)
# It intentionally EXCLUDES the UNC ``\\\\`` alternative (a JSON-escaped or
# ``%r``-repr single path doubles every separator to ``\\``, so ``\\\\`` would
# false-split it) and a bare ``Users``/``home`` boundary (indistinguishable from
# a single path's interior ``Users`` directory, which must stay whole). BOTH of
# those second-path shapes are instead caught tail-locally in steps 1/2 by
# :func:`_tail_before_reanchor`: a drive-less / POSIX home re-root is a clean
# ``Users``/``home`` tail component, and a UNC re-root is a separator run of two
# or more characters (a structural tell that needs no width comparison). Neither
# can be confused with a single path's interior structure there, so the splitter
# is free to omit both ambiguous alternatives.
_SECOND_PATH_ANCHOR_RE = re.compile(
    r"[A-Za-z]:(?:\\|/(?!/))"
    r"|(?<![\\/])%(?:USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)%[\\/]"
    r"|(?<![\\/])\$(?:env:)?\{?(?:USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)\}?[\\/]",
    re.IGNORECASE,
)

# An env-var / tilde *home or app-data root*, matched against a whole path
# component (``fullmatch``). Two consumers:
#   * :func:`_env_root_placeholder` — a leading env-rooted path with no marker
#     collapses to ``<APP_DATA>`` (APPDATA/LOCALAPPDATA) or ``<HOME>`` (the
#     home-family vars and ``~``), tail dropped.
#   * :func:`_tail_before_reanchor` — an env-var/tilde root appearing *inside* a
#     tail (a separator-introduced second path the drive/env splitter left
#     alone) re-roots and truncates the tail there.
# The name is captured so the placeholder mapping can read it; the tilde branch
# has no name and maps to ``<HOME>``. The var set matches the path anchors, so a
# directory merely *named* like an unrelated env var (e.g. ``%TEMP%``) is not
# treated as a home re-root.
_ENV_ROOT_RE = re.compile(
    r"%(?P<pct>USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)%"
    r"|\$(?:env:)?\{?(?P<dollar>USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)\}?"
    r"|~",
    re.IGNORECASE,
)

# Windows extended-length / device prefix (``\\?\C:\...`` / ``\\?\UNC\srv\...``
# / ``\\.\C:\...``, either slash direction). The ``\\`` UNC anchor already
# matches it, but left in place the ``?`` (or the device ``.``) survives the
# reducer as a stray leading component (``?<APP_DATA>/...`` / ``.<APP_DATA>/...``).
# We normalize it away up front: ``\\?\C:\...`` / ``\\.\C:\...`` -> the plain
# drive path; ``\\?\UNC\srv\share\...`` -> the equivalent ``\\srv\share\`` UNC
# path. Purely cosmetic — the username is dropped to a basename either way; the
# ``[?.]`` class covers both the ``\\?\`` extended-length and ``\\.\`` device
# spellings (``\\.\`` always names the device namespace, never a real host).
_EXT_LEN_PREFIX_RE = re.compile(r"[\\/]{2}[?.][\\/](?P<unc>UNC[\\/])?", re.IGNORECASE)


# Separator-run splitter that KEEPS the runs (capture group), so the reducer can
# inspect each run as a STRING — its length and slash direction are the
# structural tell that a tail component re-roots a second path
# (:func:`_is_reroot_run`). A bare ``[\\/]+`` split would discard that.
_SEP_SPLIT_RE = re.compile(r"([\\/]+)")


def _env_root_placeholder(comp: str) -> str | None:
    """The placeholder a leading env-var/tilde-rooted path collapses to, or
    ``None`` if ``comp`` is not such a root.

    ``APPDATA`` / ``LOCALAPPDATA`` → :data:`APP_DATA_PLACEHOLDER` (the wrapper's
    data root lives under these); ``USERPROFILE`` / ``HOMEPATH`` / ``HOMEDRIVE``,
    the ``$``-spelled home vars, and ``~`` → :data:`HOME_PLACEHOLDER`. Used by
    :func:`_reduce_single_path_token` to drop an env-rooted path's (un-vettable)
    tail to a bare placeholder rather than emit its leaf basename — which could
    itself be a glued second path's username.
    """

    m = _ENV_ROOT_RE.fullmatch(comp)
    if m is None:
        return None
    name = m.group("pct") or m.group("dollar")     # None for the ``~`` branch
    if name is not None and name.upper() in ("APPDATA", "LOCALAPPDATA"):
        return APP_DATA_PLACEHOLDER
    return HOME_PLACEHOLDER


def _is_home_root(comp: str) -> bool:
    """True if path component ``comp`` is a ``Users``/``home`` root, tolerating
    the trailing dots/spaces Windows silently coalesces away.

    Windows canonicalizes a path component by stripping its trailing dots and
    spaces, so ``C:\\Users.\\Jane``, ``C:\\Users \\Jane`` and ``C:\\Users...\\Jane``
    all resolve to the REAL ``C:\\Users\\Jane`` home — the component right after
    the root is still the account username. An exact ``comp.lower() in
    _HOME_ROOTS`` test missed those decorated spellings, so the home-root branch
    was skipped and the username leaked (as the bare-home placeholder's basename
    for a drive-rooted path, or verbatim for a tail re-root). Mirroring the OS's
    trailing-dot/space coalescing here closes that class.

    Used by BOTH :func:`_reduce_single_path_token`'s home-root step and
    :func:`_tail_before_reanchor`'s home check, so the two can't drift. The test
    only ever matches MORE than the exact form (it can drop more, never leak); a
    component merely *starting* ``users``/``home`` (``usersaccount``) is
    unaffected, and an all-dot/space component rstrips to ``""``, not a home root.
    """

    return comp.lower().rstrip(". ") in _HOME_ROOTS


def _is_reroot_run(run: str) -> bool:
    """True if the separator ``run`` before a tail component marks a glued
    second path's re-root — decided **structurally**, with no comparison to any
    "interior" width.

    Two width-FREE tells, either of which fires:

    * **Multi-character run** — ``len(run) >= 2``. A legitimate single path uses
      exactly *one* separator between components, so ANY run of two or more
      separators (a native ``\\\\`` UNC root, a JSON-escaped ``\\\\``, a
      triple/quad-escaped ``%r``-repr run, or a glued ``//`` UNC root) is either
      a re-root or an un-vettable doubled join. We cut there rather than risk
      emitting a second path's tail. This is the irreducible cost of
      width-freedom: a uniformly-doubled (JSON / ``%r``-repr) single path loses
      its tail too — accepted, because that run is *structurally
      indistinguishable* from a glued native-UNC root and the only safe call is
      to trim (see the module docstring, "Why width-free" / "Accepted tail
      loss").
    * **Forward-slash root** — two or more forward slashes (``//host/share``).
      Subsumed by the length tell for an all-forward run, but kept explicit so
      the forward-UNC / ``logs//sub`` contract survives any future change to the
      length rule.

    There is deliberately NO "wider than the path's interior" comparison and NO
    ``host\\share`` lookahead: those were the width heuristic whose irreducible
    width-2-vs-width-2 collision (a doubled interior run equals a native-UNC
    root) this function exists to remove.
    """

    if len(run) >= 2:
        return True
    if run.count("/") >= 2:
        return True
    return False


def _tail_before_reanchor(
    tail_comps: list[str],
    tail_sep_runs: list[str],
) -> list[str]:
    """Trim ``tail_comps`` at the first component that re-roots a second path.

    Steps 1-3 emit a token's tail (the components after the app-data / app-source
    marker, or after a home root's username) verbatim. A drive- or env-var-rooted
    second path is already split off upstream by :func:`_reduce_path_token`'s
    multi-path guard, but four *separator-introduced* second-path / traversal
    shapes slip past it and would leak their username if the tail were emitted
    whole. The tail is cut at the FIRST component for which ANY of these holds:

    * The component is ``..`` — a parent-traversal that Windows resolves before
      it opens the path, so it can walk back *up* out of an app-data / app-source
      tail to a home whose leaf is a username
      (``...\\ZDUltimateLegend\\..\\..\\Eve Ng`` resolves to ``C:\\Users\\Eve
      Ng``). It cannot be statically resolved without touching the filesystem, so
      the tail is cut there rather than risk re-emitting the repositioned leaf. (A
      home-rooted path collapses the *whole* reduction to ``<HOME>`` on any post-
      root ``..`` — see :func:`_reduce_single_path_token` step 4 — and never
      reaches here with a ``..`` in its tail.)
    * The component is a ``Users``/``home`` root (trailing dots/spaces tolerated
      — :func:`_is_home_root`) — a drive-less or POSIX home re-root
      (``...\\ZDUltimateLegend\\a.json\\Users\\Bob``). (An
      app-data / app-source / post-username tail never legitimately contains a
      ``Users``/``home`` component; a single path whose *interior* directory is
      named ``Users`` reduces via the home-rooted branch to its basename and
      never reaches here.)
    * Its preceding separator run re-roots structurally — :func:`_is_reroot_run`,
      i.e. two or more characters (a native or escaped ``\\\\`` UNC root) or two
      or more forward slashes (``//srv/share`` forward-UNC). A UNC second path
      has no ``Users``/``home`` component (its username sits after
      ``host\\share``), so this run is its only tell.
    * The component itself is an env-var / tilde root (``%APPDATA%`` /
      ``$env:USERPROFILE`` / ``~``) — a separator-introduced env re-root that the
      drive/env splitter leaves attached to the current path.

    Every trim is loss-free for a real single-separator app-data / app-source /
    post-username tail (which carries none of those) and leak-proof for a glued
    second path. A uniformly-doubled (JSON / repr) tail is trimmed at its first
    component by the multi-character tell — the accepted tail loss documented in
    the module docstring.
    """

    for k, comp in enumerate(tail_comps):
        if comp == "..":
            return tail_comps[:k]
        if _is_home_root(comp):
            return tail_comps[:k]
        if _is_reroot_run(tail_sep_runs[k]):
            return tail_comps[:k]
        if _env_root_placeholder(comp) is not None:
            return tail_comps[:k]
    return tail_comps


def _reduce_single_path_token(token: str) -> str:
    """Collapse ONE filesystem path (no embedded second path) to a safe form.

    :func:`_reduce_path_token` has already split a multi-path token at every
    fresh drive/env anchor, so ``token`` here is a single path. The username
    (the component after a ``Users``/``home`` root) is never part of the return
    value. See the module docstring for the reduction order (steps 1-5).
    """

    # Split on runs of separators, KEEPING the runs (``_SEP_SPLIT_RE`` captures,
    # so the result interleaves components at even indices and the separator runs
    # between them at odd indices). ``sep_runs[k]`` is the separator-run STRING
    # before ``comps[k]`` (``""`` for the first component) — kept as a string so
    # :func:`_is_reroot_run` can read its length and slash direction. Empty
    # components (from a leading separator) carry no reduction info and are
    # dropped.
    pieces = _SEP_SPLIT_RE.split(token)
    comps: list[str] = []
    sep_runs: list[str] = []
    for idx in range(0, len(pieces), 2):
        comp = pieces[idx]
        if not comp:
            continue
        comps.append(comp)
        sep_runs.append(pieces[idx - 1] if idx > 0 else "")
    if not comps:
        return token

    # 1) App-data path → <APP_DATA>/<tail>. Everything up to and including the
    #    marker (home + username included) is replaced by the placeholder. The
    #    tail is trimmed at any re-root so a second path glued onto it cannot
    #    leak (see _tail_before_reanchor).
    for i, comp in enumerate(comps):
        if comp == _APP_DIR_NAME:
            tail = _tail_before_reanchor(comps[i + 1:], sep_runs[i + 1:])
            return APP_DATA_PLACEHOLDER + ("/" + "/".join(tail) if tail else "")

    # 2) App source tree → keep from the marker on (drop the home prefix); the
    #    post-marker tail is likewise trimmed at any re-root.
    for i, comp in enumerate(comps):
        if comp in _SOURCE_MARKERS:
            return "/".join(
                [comp] + _tail_before_reanchor(comps[i + 1:], sep_runs[i + 1:])
            )

    # 3) Env-var / tilde-rooted path with NO marker → bare placeholder, tail
    #    DROPPED. A leading %USERPROFILE% / $env:APPDATA / ~ expands to a dir
    #    *containing the username*, so its tail is an arbitrary user path we
    #    cannot structurally vet — and its leaf basename could BE a glued second
    #    path's username. We emit the placeholder instead of the basename.
    #    Ordered after the marker checks so %LOCALAPPDATA%\ZDUltimateLegend\...
    #    keeps its tail via step 1.
    placeholder = _env_root_placeholder(comps[0])
    if placeholder is not None:
        return placeholder

    # 4) Home-rooted path → drop the username component, reduce the remainder.
    #    The post-username tail is trimmed at any further home / UNC / env / ``..``
    #    re-root BEFORE taking the basename, so a glued second path whose own
    #    last component is its username (``...\\Documents\\a.json\\\\srv\\share\\
    #    Dave Lee``) cannot ride out as this path's basename.
    #
    #    Interior ``.`` (current-dir) and ``..`` (parent) components are
    #    canonicalized, because Windows resolves them before opening the path and
    #    the component the OS lands on is still the account username:
    #      * ``.`` / empty are no-ops — skipped both when LOCATING the username
    #        after the root (so ``Users\\.\\<name>`` drops the real ``<name>``,
    #        not the ``.``) and when taking the basename (so a legit ``.\\file``
    #        still keeps ``file``).
    #      * a ``..`` ANYWHERE after the root collapses the whole reduction to
    #        ``<HOME>`` — the safe floor. ``Users\\Alice\\..\\Bob`` resolves to
    #        ``Users\\Bob``, repositioning the username to the leaf; it cannot be
    #        statically resolved without a syscall, so we drop to ``<HOME>``
    #        rather than risk emitting the leaf. (A ``..`` *before* the root is
    #        harmless — the root is still found and its next component dropped.)
    for i, comp in enumerate(comps[:-1]):
        if _is_home_root(comp):
            rest = comps[i + 1:]
            rest_runs = sep_runs[i + 1:]
            if ".." in rest:
                return HOME_PLACEHOLDER     # traversal repositions the username
            j = 0                           # skip interior '.' / empty no-ops
            while j < len(rest) and rest[j] in (".", ""):
                j += 1
            if j >= len(rest):
                return HOME_PLACEHOLDER      # bare home (only no-ops after root)
            safe_tail = _tail_before_reanchor(rest[j + 1:], rest_runs[j + 1:])
            safe_tail = [c for c in safe_tail if c not in (".", "")]
            if safe_tail:
                return safe_tail[-1]        # basename, username already excluded
            return HOME_PLACEHOLDER         # path was the bare home directory

    # 5) Any other drive/UNC path → basename.
    return comps[-1]


def _reduce_path_token(token: str) -> str:
    """Collapse one path-shaped token to a privacy-safe form.

    A single token can carry more than one filesystem path, merged by a
    *non-whitespace* connector the body tempering cannot see — a ``repr([...])``
    list separator (``', '``), a bare comma/semicolon, ``) (``, or no separator
    at all (a second drive letter glued onto the first path's basename). Left
    whole, steps 1/2 of the single-path reduction would re-emit the merged tail
    verbatim and leak the *second* path's username.

    So we walk the token left-to-right, splitting at each *fresh* drive/env
    anchor and reducing every segment independently via
    :func:`_reduce_single_path_token`, then concatenate the reductions with no
    separator (the connector text between two paths sits at the tail of the
    preceding segment and rides through its reduction unchanged — exactly as the
    per-segment reduction emits it). The walk is ITERATIVE: an earlier version
    recursed through :func:`scrub_paths` once per embedded path, so a token glued
    from a few hundred paths overflowed the Python stack with an uncaught
    ``RecursionError`` (reachable from the crash reporter's joined-traceback
    scrub and the diagnostics event-log scrub). This loop uses O(1) Python stack
    no matter how many paths the token carries; the per-segment leak-protection
    logic is unchanged.
    """

    # Normalize a Windows extended-length / device prefix first, so it does not
    # leave a stray ``?``/``.`` component in the output (cosmetic; see
    # _EXT_LEN_PREFIX_RE). \\?\C:\... / \\.\C:\... -> C:\... ; \\?\UNC\srv\... -> \\srv\...
    m = _EXT_LEN_PREFIX_RE.match(token)
    if m is not None:
        token = ("\\\\" if m.group("unc") else "") + token[m.end():]

    segments = []
    rest = token
    while rest:
        anchor = _PATH_ANCHOR_RE.match(rest)
        # Search for the NEXT fresh anchor past the leading one (``anchor.end()``
        # so the leading anchor can't re-match itself). ``second.start() >= 1``
        # always, so ``rest`` strictly shrinks each pass and the loop terminates.
        second = _SECOND_PATH_ANCHOR_RE.search(rest, anchor.end() if anchor else 1)
        if second is None:
            segments.append(_reduce_single_path_token(rest))
            break
        cut = second.start()
        segments.append(_reduce_single_path_token(rest[:cut]))
        rest = rest[cut:]
    return "".join(segments)


def scrub_paths(text: str) -> str:
    """Return ``text`` with every embedded filesystem path scrubbed.

    Non-path text is returned unchanged. Safe to run over an entire document
    (Markdown, JSON, a log line, a crash traceback) — only the path-shaped
    spans are rewritten.
    """

    if not text:
        return text
    return _PATH_TOKEN_RE.sub(lambda m: _reduce_path_token(m.group(0)), text)


def scrub_value(value: object) -> str:
    """Coerce ``value`` to ``str`` and scrub it; ``None``/empty → ``""``.

    Convenience wrapper for the freeform fields (notes, profile names, device
    strings) that flow into shareable artifacts and may be ``None`` or a
    non-string.
    """

    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    return scrub_paths(text)


class PathScrubbingFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that scrubs paths from each emitted line.

    Attach it to the rotating app-log handler so the log file — which
    ``SUPPORT.md`` asks users to attach to bug reports — never records the
    operator's home path / account username. One handler-level chokepoint
    covers every log source: startup paths, service ``base_dir=%r`` lines, and
    ``logger.exception()`` output, since the scrubber runs over the *fully
    formatted* record (message + args + any appended traceback / stack frames).
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub_paths(super().format(record))
