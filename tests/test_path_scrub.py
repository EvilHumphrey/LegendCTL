"""Tests for the shared filesystem-path scrubber (:mod:`zd_app.services.path_scrub`).

The scrubber is the single chokepoint every user-shareable artifact routes
through. The regression these tests pin: the old per-module regexes stopped at
the first whitespace, so a Windows display-name account (``C:\\Users\\Jane
Doe\\...``) leaked the username's first name verbatim. The scrubber anchors on
the ``Users``/``home`` root and drops the *whole* username component (bounded
by separators, not spaces), so a spaced — or even two-spaced — name never
survives.
"""

from __future__ import annotations

import logging
import unittest

from zd_app.services.path_scrub import (
    APP_DATA_PLACEHOLDER,
    HOME_PLACEHOLDER,
    PathScrubbingFormatter,
    scrub_paths,
    scrub_value,
)


class SpacedUsernameTests(unittest.TestCase):
    """The core fix: a username with interior space(s) is dropped whole."""

    def test_spaced_username_with_app_data_marker(self) -> None:
        out = scrub_paths(
            r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\profile.json"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/profile.json")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_spaced_username_without_marker_reduces_to_basename(self) -> None:
        out = scrub_paths(r"C:\Users\Jane Doe\Documents\foo.txt")
        self.assertEqual(out, "foo.txt")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)
        self.assertNotIn(r"C:\Users", out)

    def test_two_space_username(self) -> None:
        out = scrub_paths(r"C:\Users\Mary Jane Watson\Documents\x.txt")
        self.assertEqual(out, "x.txt")
        for token in ("Mary", "Jane", "Watson"):
            self.assertNotIn(token, out)

    def test_hyphenated_and_dotted_username(self) -> None:
        out = scrub_paths(r"C:\Users\jane-doe.admin\Documents\report.pdf")
        self.assertEqual(out, "report.pdf")
        self.assertNotIn("jane-doe.admin", out)

    def test_posix_spaced_username(self) -> None:
        out = scrub_paths("/Users/Jane Doe/Documents/secret.json")
        self.assertEqual(out, "secret.json")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_bare_home_directory_collapses_to_placeholder(self) -> None:
        # The path IS the home dir — the username is the last component, so a
        # naive basename would *be* the leak. It must collapse instead.
        out = scrub_paths(r"C:\Users\Jane Doe")
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_username_with_legal_punctuation_dropped_whole(self) -> None:
        # Apostrophes, parens, square brackets, and braces are all LEGAL in
        # Windows account/folder names ("O'Connor", "Jane (Admin)",
        # "Jane[Work]"). The old terminator class listed them, so the path
        # token stopped mid-name: the leading fragment collapsed to <HOME> and
        # the trailing surname fragment ("Connor", "Admin", "Work") leaked
        # verbatim into every shared artifact. Fail-on-base regression: the
        # whole username component must be dropped to the basename.
        leaked_fragments = {
            "O'Connor": ("Connor",),
            "Jane (Admin)": ("Admin",),
            "O'Brien O'Malley": ("Brien", "Malley"),
            "Jane[Work]": ("Work",),
            "Jane{x}": (),  # brace content proven gone by the equality assert
        }
        for username, fragments in leaked_fragments.items():
            with self.subTest(username=username):
                out = scrub_paths(rf"C:\Users\{username}\Documents\foo.txt")
                self.assertEqual(out, "foo.txt", f"{username!r} not reduced")
                self.assertNotIn(username, out)
                for fragment in fragments:
                    self.assertNotIn(fragment, out)
                # No path-token punctuation should survive in the basename.
                for char in "'()[]{}":
                    self.assertNotIn(char, out)

    def test_apostrophe_username_with_app_data_marker(self) -> None:
        # The app-data collapse must still fire when the username carries an
        # apostrophe — on base the token truncated at the ' and never reached
        # the ZDUltimateLegend marker, so the <APP_DATA> reduction was defeated.
        out = scrub_paths(
            r"C:\Users\O'Brien\AppData\Roaming\ZDUltimateLegend\logs"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs")
        self.assertNotIn("Brien", out)

    def test_posix_apostrophe_username_reduces_to_basename(self) -> None:
        out = scrub_paths("/Users/o'neil/Documents/foo.txt")
        self.assertEqual(out, "foo.txt")
        self.assertNotIn("neil", out)


class RootedWindowsHomeTests(unittest.TestCase):
    """Drive-LESS Windows home roots (``\\Users\\...`` / ``\\home\\...``).

    A rooted home path that starts at a bare separator — what ``%HOMEPATH%``
    expands to, and what shows up in some env-derived strings — has no drive
    letter and no leading ``//``, so neither the drive-letter nor the POSIX
    anchor matched it. The username leaked verbatim (fail-on-base): on base
    ``\\Users\\Jane Doe\\...`` was returned UNCHANGED. The rooted-home anchor
    drops the whole username component, both slash directions.
    """

    def test_rooted_backslash_home_drops_username(self) -> None:
        out = scrub_paths(r"note \Users\Jane Doe\Documents\secret.json")
        self.assertEqual(out, "note secret.json")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_rooted_home_root_word_lowercase(self) -> None:
        out = scrub_paths(r"\home\Jane Doe\stuff\f.txt")
        self.assertEqual(out, "f.txt")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_rooted_home_non_latin_username(self) -> None:
        # A non-Latin display-name account must be dropped whole, same as a
        # spaced Latin one. (Kept out of a literal repr() so the source file
        # stays ASCII-clean; the equality assert proves the name is gone.)
        username = "李 雷"  # "Li Lei"
        out = scrub_paths("\\Users\\" + username + "\\Documents\\secret.json")
        self.assertEqual(out, "secret.json")
        self.assertNotIn(username, out)

    def test_rooted_home_mixed_slash_directions(self) -> None:
        for path in (
            r"\Users/Jane Doe/Documents/secret.json",
            r"/Users\Jane Doe\Documents\secret.json",
        ):
            with self.subTest(path=path):
                out = scrub_paths(path)
                self.assertEqual(out, "secret.json")
                self.assertNotIn("Jane", out)
                self.assertNotIn("Doe", out)

    def test_rooted_home_with_app_data_marker(self) -> None:
        out = scrub_paths(
            r"\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\p.json"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/p.json")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_rooted_users_midsentence_reduces_to_basename(self) -> None:
        # A ``\Users\...`` fragment in prose reduces to its basename (the
        # username component is still dropped) — never leaving ``\Users`` or the
        # username verbatim. Same shape as the pre-existing POSIX behaviour.
        out = scrub_paths(r"stored at \Users\admin\notes.txt today")
        self.assertNotIn(r"\Users", out)
        self.assertNotIn("admin", out)
        self.assertTrue(out.startswith("stored at "))
        self.assertIn("notes.txt", out)


class HomeMarkerVariantTests(unittest.TestCase):
    """%USERPROFILE%, ~/, and UNC home forms are all handled."""

    def test_userprofile_env_var(self) -> None:
        # An env-var home root with no app-data/source marker drops its
        # un-vettable tail to <HOME> — a glued second path's username could
        # otherwise ride out as the leaf basename (see Class A in the closure
        # matrix). The username's own home is never the leak; the tail might be.
        out = scrub_paths(r"%USERPROFILE%\Documents\secret.txt")
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("%USERPROFILE%", out)
        self.assertNotIn("secret.txt", out)

    def test_appdata_env_var_with_marker(self) -> None:
        # The marker check precedes the env-root drop, so a real data path keeps
        # its (single-separator) tail.
        out = scrub_paths(r"%APPDATA%\ZDUltimateLegend\logs\zd_wrapper.log")
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs/zd_wrapper.log")

    def test_tilde_home(self) -> None:
        # ``~`` is a home anchor → tail dropped to <HOME> (no marker present).
        out = scrub_paths("~/Documents/secret.json")
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("secret.json", out)

    def test_unc_path_drops_user_component(self) -> None:
        out = scrub_paths(r"\\fileserver\share\Jane Doe\Documents\foo.txt")
        self.assertEqual(out, "foo.txt")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_posix_home_with_interior_space_dir_and_marker(self) -> None:
        # macOS "Application Support" has an interior space *and* the path ends
        # at the app-data marker — both handled in one pass.
        out = scrub_paths(
            "/Users/jane/Library/Application Support/ZDUltimateLegend/rp/x.json"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/rp/x.json")
        self.assertNotIn("jane", out)


class SourceFramePreservationTests(unittest.TestCase):
    """Crash-traceback frames keep app-relative context (zd_app / _internal)."""

    def test_zd_app_frame_kept_from_package_root(self) -> None:
        out = scrub_paths(r"C:\Users\Jane Doe\proj\zd_app\services\foo.py")
        self.assertEqual(out, "zd_app/services/foo.py")
        self.assertNotIn("Jane", out)

    def test_internal_frozen_frame_kept(self) -> None:
        out = scrub_paths(
            r"C:\Users\Jane Doe\AppData\Local\Temp\_MEI42\_internal\zd_app\m.py"
        )
        self.assertEqual(out, "_internal/zd_app/m.py")
        self.assertNotIn("Jane", out)

    def test_quoted_traceback_frame(self) -> None:
        line = 'File "C:\\Users\\Jane Doe\\proj\\zd_app\\app_shell.py", line 5, in run'
        out = scrub_paths(line)
        self.assertIn('"zd_app/app_shell.py"', out)
        self.assertNotIn("Jane", out)


class NonPathPassthroughTests(unittest.TestCase):
    """Plain (non-path) text is returned unchanged — no over-matching."""

    def test_controller_name_unchanged(self) -> None:
        self.assertEqual(scrub_paths("ZD Ultimate Legend"), "ZD Ultimate Legend")

    def test_profile_name_unchanged(self) -> None:
        self.assertEqual(scrub_paths("Apex"), "Apex")

    def test_url_not_mangled(self) -> None:
        text = "see https://example.com/repo/path for docs"
        self.assertEqual(scrub_paths(text), text)

    def test_non_home_system_path_left_alone(self) -> None:
        # The POSIX form only anchors on /Users/ and /home/, so system paths
        # carry no PII and are untouched.
        self.assertEqual(scrub_paths("/usr/lib/libfoo.so"), "/usr/lib/libfoo.so")

    def test_embedded_path_in_sentence(self) -> None:
        out = scrub_paths(
            "Loaded profile from "
            r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\wrapper_profiles\p.json"
            " just now"
        )
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/wrapper_profiles/p.json", out)
        self.assertTrue(out.startswith("Loaded profile from "))
        self.assertNotIn("Jane", out)


class ScrubValueTests(unittest.TestCase):
    def test_none_and_empty(self) -> None:
        self.assertEqual(scrub_value(None), "")
        self.assertEqual(scrub_value(""), "")

    def test_non_string_coerced(self) -> None:
        self.assertEqual(scrub_value(42), "42")

    def test_path_value_scrubbed(self) -> None:
        self.assertEqual(scrub_value(r"C:\Users\Jane Doe\x\y.bin"), "y.bin")


class PathScrubbingFormatterTests(unittest.TestCase):
    """The log formatter scrubs message, args, AND appended tracebacks."""

    def _format(self, record: logging.LogRecord) -> str:
        fmt = PathScrubbingFormatter("%(levelname)s %(name)s: %(message)s")
        return fmt.format(record)

    def test_message_arg_path_scrubbed(self) -> None:
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="File logger attached at %s",
            args=(r"C:\Users\Jane Doe\AppData\Roaming\ZDUltimateLegend\logs\zd_wrapper.log",),
            exc_info=None,
        )
        out = self._format(record)
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/logs/zd_wrapper.log", out)
        self.assertNotIn("Jane", out)
        self.assertNotIn(r"C:\Users", out)

    def test_base_dir_repr_path_scrubbed(self) -> None:
        record = logging.LogRecord(
            name="svc", level=logging.INFO, pathname=__file__, lineno=1,
            msg="Service constructed base_dir=%r",
            args=(r"C:\Users\Jane Doe\proj\zd_data\wear_ledger",),
            exc_info=None,
        )
        out = self._format(record)
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_rooted_home_path_arg_scrubbed(self) -> None:
        # A drive-less rooted home path flowing through the app-log formatter
        # must drop the username, same as the drive-letter form.
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="loaded %s",
            args=(r"\Users\Jane Doe\Documents\secret.json",),
            exc_info=None,
        )
        out = self._format(record)
        self.assertIn("secret.json", out)
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)
        self.assertNotIn(r"\Users", out)

    def test_exception_traceback_scrubbed(self) -> None:
        try:
            raise FileNotFoundError(r"C:\Users\Jane Doe\AppData\config.json")
        except FileNotFoundError:
            import sys
            record = logging.LogRecord(
                name="t", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="boom", args=(), exc_info=sys.exc_info(),
            )
        out = self._format(record)
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)
        # The traceback block survives (the exception type is still reported);
        # only the home path in the exception message is reduced to a basename.
        self.assertIn("FileNotFoundError", out)


class MultiPathLineTests(unittest.TestCase):
    """Two paths on one line, joined by terminator-free prose, reduce each
    independently — the second path's username can't ride along in the first
    path's token tail.

    Fail-on-base: a space is not a path terminator, so on base the whole line
    is one token and the reducer only collapses the FIRST path; the second
    home path's username (``Alice Doe``) leaked verbatim into the app log and
    the crash exception-summary (both SUPPORT-shared artifacts).
    """

    def test_copy_connector_two_app_data_paths(self) -> None:
        out = scrub_paths(
            r"copy C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\p.json"
            r" to C:\Users\Alice Doe\Documents\export.json"
        )
        self.assertEqual(out, "copy <APP_DATA>/p.json to export.json")
        self.assertNotIn("Alice", out)
        self.assertNotIn("Doe", out)

    def test_wrote_connector_source_then_home(self) -> None:
        out = scrub_paths(
            r"C:\Users\Alice Doe\proj\zd_app\foo.py"
            r" wrote C:\Users\Alice Doe\Documents\secret.json"
        )
        self.assertEqual(out, "zd_app/foo.py wrote secret.json")
        self.assertNotIn("Alice", out)
        self.assertNotIn("Doe", out)

    def test_and_connector_two_home_paths(self) -> None:
        out = scrub_paths(
            r"C:\Users\Alice Doe\Documents\a.json"
            r" and C:\Users\Alice Doe\Documents\b.json"
        )
        self.assertEqual(out, "a.json and b.json")
        self.assertNotIn("Alice", out)
        self.assertNotIn("Doe", out)

    def test_env_var_path_then_home_path(self) -> None:
        # An env-var-rooted path (whitespace-split) followed by a drive-letter
        # home path: the env path drops its tail to <HOME>, the drive home path
        # reduces to its basename, and the home username is still dropped. (The
        # connector word "and" trails the env path with no terminator, so the
        # tokenizer pulls it into the env token's tail, where the env-root drop
        # discards it — leak-proof, just a connector word lost.)
        out = scrub_paths(
            r"$env:USERPROFILE\Documents\a.txt"
            r" and C:\Users\Alice Doe\Documents\b.txt"
        )
        self.assertEqual(out, f"{HOME_PLACEHOLDER} b.txt")
        self.assertNotIn("Alice", out)
        self.assertNotIn("USERPROFILE", out)


class SinglePathNoFragmentTests(unittest.TestCase):
    """A single path that *contains* an interior anchor-shaped run must stay
    one token and reduce as a whole — multi-path tempering must NOT split it.

    These pin the reason the body tempers only on a *whitespace* gap before the
    next anchor: an unconditional ``(?!anchor)`` temper would fragment each of
    these (the ``\\`` / ``\\Users\\`` cases mid-path), defeating the
    ``<APP_DATA>`` collapse or leaking the interior ``Users`` child component.
    """

    def test_repr_doubled_backslash_home_path_collapses_to_home(self) -> None:
        # ``logger.info("base_dir=%r", path)`` emits doubled backslashes (every
        # separator width 2). Under the width-free trim a uniformly-doubled home
        # path loses its tail and collapses to <HOME> (accepted tail loss — see
        # the module docstring); the username is dropped and nothing fragments
        # into ``C:<HOME>\\proj\\...``. The ``%r`` closing quote is path-legal, so
        # it is part of the dropped ``wear_ledger'`` component and goes with the
        # tail (output ends ``<HOME>`` with no closing quote — cosmetic only).
        out = scrub_paths(
            "base_dir='C:\\\\Users\\\\Jane Doe\\\\proj\\\\zd_data\\\\wear_ledger'"
        )
        self.assertEqual(out, f"base_dir='{HOME_PLACEHOLDER}")
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)

    def test_json_escaped_app_data_collapses(self) -> None:
        # A diagnostic-bundle JSON member is scrubbed as raw text, so its path
        # values carry JSON-escaped ``\\`` separators (width 2). The app-data
        # collapse still fires; the width-free trim drops the doubled tail, so it
        # reduces to the bare placeholder (accepted tail loss, never a leak).
        out = scrub_paths(
            '"path": "C:\\\\Users\\\\Jane Doe\\\\AppData\\\\Roaming'
            '\\\\ZDUltimateLegend\\\\profile.json"'
        )
        self.assertEqual(out, '"path": "<APP_DATA>"')
        self.assertNotIn("Jane", out)

    def test_interior_users_directory_reduces_to_basename(self) -> None:
        # ``Users`` here is an ordinary interior directory, not the home root.
        # The path is a single token → basename ``file.txt``; the child of the
        # interior ``Users`` (``Bob``) must not leak as a re-anchored fragment.
        out = scrub_paths(r"C:\data\Users\Bob\file.txt")
        self.assertEqual(out, "file.txt")
        self.assertNotIn("Bob", out)


class EnvVarHomeFormTests(unittest.TestCase):
    """Shell/PowerShell env-var home spellings ($VAR, $env:VAR, ${VAR}).

    The reducer treats the env var as an env-var ROOT: an app-data marker still
    collapses to ``<APP_DATA>`` (and keeps a single-separator tail), but an
    env-rooted path with NO marker drops its (un-vettable) tail to a bare
    placeholder — ``<APP_DATA>`` for APPDATA/LOCALAPPDATA, ``<HOME>`` for the
    home-family vars — rather than emitting a leaf basename that could be a
    glued second path's username.
    """

    def test_dollar_localappdata_with_marker(self) -> None:
        out = scrub_paths(r"$LOCALAPPDATA\ZDUltimateLegend\logs\zd_wrapper.log")
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs/zd_wrapper.log")

    def test_dollar_env_userprofile_drops_tail_to_home(self) -> None:
        # No marker → the env home root drops its tail to <HOME>.
        out = scrub_paths(r"$env:USERPROFILE\Documents\secret.txt")
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("USERPROFILE", out)
        self.assertNotIn("secret.txt", out)

    def test_braced_localappdata_with_marker(self) -> None:
        out = scrub_paths(r"${LOCALAPPDATA}\ZDUltimateLegend\x.json")
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/x.json")

    def test_forward_slash_env_var(self) -> None:
        out = scrub_paths(r"$APPDATA/ZDUltimateLegend/rp/x.json")
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/rp/x.json")


class MergedTokenMultiPathTests(unittest.TestCase):
    """Two+ paths merged into ONE token by a *non-whitespace* connector.

    The body tempering only splits paths separated by a whitespace gap. A
    repr-list separator (``', '``), a bare comma/semicolon, ``) (``, or zero
    separator at all (the second drive letter glued straight onto the first
    path's basename) all merge a second path into the first path's token — and
    on base ``2cc07e9`` steps 1/2 of the reducer re-emit that merged tail
    verbatim, leaking the SECOND path's username (``Bob Roe``) into every
    SUPPORT-shared artifact (app log, crash summary, diagnostic bundle).

    Fail-on-base: the repr-list, comma, and glued cases below all return the
    second path's ``C:/Users/Bob Roe/...`` verbatim on base; the reducer-side
    multi-path guard splits at the second drive/env anchor so each path reduces
    independently. Every case asserts the 2nd username is ABSENT *and* the 1st
    path still reduces to its expected privacy-safe form.
    """

    # First-path variants, keyed by the reducer branch they exercise.
    _APP_DATA_FIRST = (
        r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json",
        f"{APP_DATA_PLACEHOLDER}/a.json",          # step 1
    )
    _SOURCE_FIRST = (
        r"C:\Users\Alice Doe\proj\zd_app\services\foo.py",
        "zd_app/services/foo.py",                  # step 2
    )
    # Second path: a drive-rooted home path whose username must never survive.
    _HOME_SECOND = r"C:\Users\Bob Roe\b.json"

    @staticmethod
    def _merge(first: str, second: str, sep: str) -> str:
        if sep == "repr_list":
            return "['%s', '%s']" % (first, second)   # repr() of a path list
        connector = {"comma": ",", "semicolon": ";", "paren": ") (", "glued": ""}[sep]
        return first + connector + second

    def test_second_username_never_rides_along(self) -> None:
        for label, (first, reduced_first) in (
            ("app_data_first", self._APP_DATA_FIRST),
            ("source_first", self._SOURCE_FIRST),
        ):
            for sep in ("repr_list", "comma", "semicolon", "paren", "glued"):
                with self.subTest(first=label, sep=sep):
                    out = scrub_paths(self._merge(first, self._HOME_SECOND, sep))
                    # The second path's username component is gone…
                    self.assertNotIn("Bob", out)
                    self.assertNotIn("Roe", out)
                    # …and the first path still reduced to its safe form.
                    self.assertIn(reduced_first, out)
                    # The home root of the second path never survives either.
                    self.assertNotIn("Users", out)

    def test_three_paths_mixed_markers_and_home(self) -> None:
        # app-data , source ', ' home — three paths, mixed connectors, one line.
        a, _ = self._APP_DATA_FIRST
        s, _ = self._SOURCE_FIRST
        out = scrub_paths(a + "," + s + "', '" + self._HOME_SECOND)
        self.assertEqual(out, "<APP_DATA>/a.json,zd_app/services/foo.py', 'b.json")
        for token in ("Alice", "Bob", "Doe", "Roe"):
            self.assertNotIn(token, out)

    def test_env_var_second_path_after_app_data(self) -> None:
        # The merged second path is env-var-rooted (exercises the guard's env
        # alternative, not just the drive alternative). The split-off env path
        # drops its tail to <HOME>.
        a, reduced = self._APP_DATA_FIRST
        out = scrub_paths(a + "," + r"%USERPROFILE%\Documents\secret.txt")
        self.assertIn(reduced, out)
        self.assertIn(HOME_PLACEHOLDER, out)
        self.assertNotIn("secret.txt", out)
        self.assertNotIn("USERPROFILE", out)

    def test_drive_less_home_second_glued_is_truncated(self) -> None:
        # A *drive-less* home path glued onto an app-data tail has no drive/env
        # anchor for the guard to split on — it survives as a clean ``Users``
        # component in the emitted tail. Step 1's tail-trim drops it (loss-free
        # for real app-data tails, leak-proof for this glued second path).
        a, _ = self._APP_DATA_FIRST
        glued = a + "\\Users\\Bob Roe\\b.json"
        out = scrub_paths(glued)
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/a.json")
        self.assertNotIn("Bob", out)
        self.assertNotIn("Roe", out)


class MergedTokenPreservationTests(unittest.TestCase):
    """The multi-path guard must NOT damage single paths or already-safe lines.

    Pins that the reducer-side split fires ONLY on a genuine second drive/env
    anchor — never on a single path's interior structure, JSON-escaping, UNC
    double-slash, or a home-first line that was already leak-free on base.
    """

    def test_whitespace_tab_and_newline_still_split(self) -> None:
        # A tab / newline is a real terminator, so the two paths are separate
        # tokens — each reduces, and the second username is still dropped.
        a = r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
        h = r"C:\Users\Bob Roe\b.json"
        for gap in ("\t", "\n"):
            with self.subTest(gap=repr(gap)):
                out = scrub_paths(a + gap + h)
                self.assertIn(f"{APP_DATA_PLACEHOLDER}/a.json", out)
                self.assertIn("b.json", out)
                self.assertNotIn("Bob", out)
                self.assertNotIn("Roe", out)

    def test_interior_home_directory_reduces_to_basename(self) -> None:
        # ``home`` as an ordinary interior directory (not a re-anchor): the path
        # is a single token reduced to its basename; the guard must not split it.
        out = scrub_paths(r"C:\data\home\bob\file.txt")
        self.assertEqual(out, "file.txt")
        self.assertNotIn("bob", out)

    def test_unc_two_level_share_not_split(self) -> None:
        # A two-level UNC share has a ``\\`` that the guard deliberately ignores
        # (a JSON-escaped single path doubles every separator to ``\\``). It
        # stays one token and reduces to its basename.
        out = scrub_paths(r"\\server\share\file.txt")
        self.assertEqual(out, "file.txt")

    def test_json_escaped_single_path_not_fragmented_by_guard(self) -> None:
        # Every separator is a doubled ``\\``; none is a second drive anchor, so
        # the guard leaves the single path whole. The app-data collapse fires
        # and the width-free trim drops the doubled tail → bare placeholder
        # (accepted tail loss); crucially the path is not *fragmented*.
        out = scrub_paths(
            "C:\\\\Users\\\\Jane Doe\\\\AppData\\\\Roaming"
            "\\\\ZDUltimateLegend\\\\logs\\\\zd_wrapper.log"
        )
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self.assertNotIn("Jane", out)

    def test_home_first_glued_stays_safe(self) -> None:
        # Home path glued to a home path: leak-free on base (step 3 returns the
        # final basename) and still leak-free after the split — neither username
        # survives.
        out = scrub_paths(
            r"C:\Users\Alice Doe\Documents\a.json" + r"C:\Users\Bob Roe\b.json"
        )
        for token in ("Alice", "Bob", "Doe", "Roe", "Users"):
            self.assertNotIn(token, out)

    def test_legit_app_data_tail_with_subdirs_preserved(self) -> None:
        # A legitimate multi-component app-data tail (no ``Users``/``home``
        # component) must survive the step-1 tail-trim intact.
        out = scrub_paths(
            r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\logs\sub\zd_wrapper.log"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs/sub/zd_wrapper.log")
        self.assertNotIn("Alice", out)


class DeepMultiPathRecursionTests(unittest.TestCase):
    """A token gluing very many paths must not overflow the Python stack.

    The reducer's multi-path split used to recurse through :func:`scrub_paths`
    once per embedded path, adding one Python stack frame per path. A token
    gluing a few hundred non-whitespace-joined paths therefore raised an
    *uncaught* ``RecursionError`` — reachable, unguarded, from the crash reporter
    (it scrubs a full joined traceback) and the diagnostics event-log scrub (it
    scrubs freeform event-log text). Fail-on-base (``207a543``): ``n=500``
    comma-glued paths already raise. The split is now iterative — O(1) Python
    stack regardless of path count — so ``n`` here is well past the default
    recursion limit (~1000) and every username is still dropped, for every
    connector shape (bare comma, semicolon, zero-separator glue).
    """

    N = 2000

    def _glued(self, joiner: str) -> str:
        # Username "Jane{i}Doe" is interior to each home path, so any leak would
        # surface the literal "Jane"/"Doe" (and a re-anchor slip would surface
        # the "Users" root).
        return joiner.join(
            rf"C:\Users\Jane{i}Doe\Documents\f{i}.txt" for i in range(self.N)
        )

    def _assert_deep_safe(self, joiner: str) -> None:
        # The bare call must not raise RecursionError.
        out = scrub_paths(self._glued(joiner))
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)
        self.assertNotIn("Users", out)

    def test_deep_comma_glue(self) -> None:
        self._assert_deep_safe(",")

    def test_deep_semicolon_glue(self) -> None:
        self._assert_deep_safe(";")

    def test_deep_zero_separator_glue(self) -> None:
        self._assert_deep_safe("")

    def test_deep_recursion_would_raise_on_base_shape(self) -> None:
        # Pin the exact fail-on-base shape from the bug report: n=500 comma-glued
        # paths. On base this raised RecursionError; with the iterative split it
        # completes and drops every username.
        paths = ",".join(
            rf"C:\Users\Jane{i}Doe\f{i}.txt" for i in range(500)
        )
        out = scrub_paths(paths)  # must not raise
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)


class EnvVarInsideUsernameTests(unittest.TestCase):
    """An env-var-shaped component is *legal* inside a Windows path, so a
    username (or directory) literally spelled ``%APPDATA%`` / ``$env:USERPROFILE``
    must not be mistaken for a fresh second path and split off.

    Fail-on-base (``207a543``): the reducer's second-path guard matched an env
    var *anywhere* — including the component right after a ``Users``/``home``
    root — so ``C:\\Users\\%APPDATA%\\Documents\\f.txt`` split into a stray
    ``Users`` + basename and returned ``Usersf.txt`` (no username leak, but
    mangled output). Requiring the env-var second-path anchor to sit at a
    component boundary (not immediately after a ``\\``/``/`` separator) keeps the
    single path whole. The drive alternative is deliberately *not* boundary-
    gated — a ``:`` is illegal mid-name, so a glued drive is always a fresh path.
    """

    def test_env_var_username_reduces_to_clean_basename(self) -> None:
        out = scrub_paths(r"C:\Users\%APPDATA%\Documents\f.txt")
        self.assertEqual(out, "f.txt")
        self.assertNotIn("Users", out)
        self.assertNotIn("APPDATA", out)

    def test_dollar_env_username_reduces_to_clean_basename(self) -> None:
        out = scrub_paths(r"C:\Users\$env:USERPROFILE\Documents\g.txt")
        self.assertEqual(out, "g.txt")
        self.assertNotIn("Users", out)
        self.assertNotIn("USERPROFILE", out)

    def test_rooted_home_env_var_username(self) -> None:
        out = scrub_paths(r"\Users\%LOCALAPPDATA%\Documents\h.txt")
        self.assertEqual(out, "h.txt")
        self.assertNotIn("Users", out)

    def test_app_data_marker_after_env_var_username(self) -> None:
        # The single path still collapses to <APP_DATA> when it carries the
        # marker, even though the username component is spelled like an env var
        # (the whole pre-marker prefix, username included, is dropped).
        out = scrub_paths(
            r"C:\Users\%APPDATA%\AppData\Roaming\ZDUltimateLegend\p.json"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/p.json")

    def test_legit_env_var_second_path_still_splits(self) -> None:
        # Regression guard for the *other* direction: a genuine env-var-rooted
        # second path (introduced by a comma connector, not a separator) must
        # still split off and reduce independently — to <HOME> (its tail is
        # dropped), never leaking a glued username.
        out = scrub_paths(
            r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
            r",%USERPROFILE%\Documents\secret.txt"
        )
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/a.json", out)
        self.assertIn(HOME_PLACEHOLDER, out)
        self.assertNotIn("secret.txt", out)
        self.assertNotIn("Alice", out)
        self.assertNotIn("USERPROFILE", out)


class UncSecondPathLeakTests(unittest.TestCase):
    """The fix: a UNC second path (``\\\\server\\share\\<user>\\...``) glued by a
    *non-whitespace* connector onto an app-data / source first path no longer
    leaks its username through steps 1/2's emitted tail.

    Fail-on-base (``d9c3937``): ``_SECOND_PATH_ANCHOR_RE`` omits the ambiguous
    ``\\\\`` (a JSON-escaped single path doubles every separator to ``\\``), and
    ``_tail_before_reanchor`` trimmed only at a ``Users``/``home`` component —
    which a UNC ``server\\share\\<user>`` path lacks — so the UNC username rode
    out verbatim in the app-data/source tail. The exact spec repro returned
    ``<APP_DATA>/a.json,/fileserver/share/Dave Lee/f.txt`` on base. The tail trim
    now cuts at any structural re-root — here the glued UNC root's separator run
    of two or more characters (:func:`_is_reroot_run`) — closing the leak with
    no width comparison.
    """

    _UNC_SECOND = r"\\fileserver\share\Dave Lee\secret.txt"

    def test_exact_unc_repro_from_spec(self) -> None:
        # The verbatim repro from the lane spec; leaked "Dave Lee" on base.
        out = scrub_paths(
            r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
            r",\\fileserver\share\Dave Lee\f.txt"
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/a.json,")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_unc_after_app_data_marker_each_connector(self) -> None:
        first = r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
        # The connector text rides at the tail of the (un-split) first path and
        # is emitted verbatim — exactly as for the drive/env multi-path guard.
        expected = {
            ",": f"{APP_DATA_PLACEHOLDER}/a.json,",
            ";": f"{APP_DATA_PLACEHOLDER}/a.json;",
            ") (": f"{APP_DATA_PLACEHOLDER}/a.json) (",
            "": f"{APP_DATA_PLACEHOLDER}/a.json",
        }
        for connector, want in expected.items():
            with self.subTest(connector=repr(connector)):
                out = scrub_paths(first + connector + self._UNC_SECOND)
                self.assertEqual(out, want)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_unc_after_source_marker_each_connector(self) -> None:
        first = r"C:\Users\Alice Doe\proj\zd_app\services\foo.py"
        expected = {
            ",": "zd_app/services/foo.py,",
            ";": "zd_app/services/foo.py;",
            ") (": "zd_app/services/foo.py) (",
            "": "zd_app/services/foo.py",
        }
        for connector, want in expected.items():
            with self.subTest(connector=repr(connector)):
                out = scrub_paths(first + connector + self._UNC_SECOND)
                self.assertEqual(out, want)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_unc_zero_glue_right_after_marker_collapses(self) -> None:
        # The UNC is glued straight onto the marker's own separator: the entire
        # tail is the second path, so the app-data path collapses to the bare
        # placeholder with nothing riding along.
        out = scrub_paths(
            r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend"
            r"\\fileserver\share\Dave Lee\secret.txt"
        )
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self.assertNotIn("Dave", out)

    def test_unc_after_app_data_control_connectors(self) -> None:
        # The two CONTROL connectors. A single space is split by the body's
        # whitespace tempering (the ``\\`` UNC anchor is recognized), so the UNC
        # becomes its own token and reduces to its basename. The repr-list
        # ``', '`` is NOT whitespace-split (a quote sits between the space and the
        # ``\\``), so it rides into the tail and is closed by the same trim —
        # i.e. on base the repr-list UNC case ALSO leaked, and is fixed here too.
        first = r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
        space_out = scrub_paths(first + " " + self._UNC_SECOND)
        self.assertEqual(space_out, f"{APP_DATA_PLACEHOLDER}/a.json secret.txt")
        self.assertNotIn("Dave", space_out)
        repr_out = scrub_paths("['%s', '%s']" % (first, self._UNC_SECOND))
        self.assertNotIn("Dave", repr_out)
        self.assertNotIn("Lee", repr_out)
        self.assertIn(f"{APP_DATA_PLACEHOLDER}/a.json", repr_out)


class SecondPathUsernameClosureMatrixTests(unittest.TestCase):
    """Exhaustive class-closure matrix: NO second path's username survives, over
    the full cross product of (second-path anchor type × connector × first-path
    reduction step). Pins the whole *second-path* leak class shut — not just the
    UNC repro that motivated the fix — so a future regression in any one cell is
    caught. 6 anchors × 6 connectors × 3 first-path steps = 108 combinations.
    """

    # First paths, keyed by the reducer branch (step) they exercise.
    _FIRST = {
        "app_data": r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json",
        "source":   r"C:\Users\Alice Doe\proj\zd_app\services\foo.py",
        "home":     r"C:\Users\Alice Doe\Documents\a.json",
    }
    # Second paths, keyed by anchor type. Each carries the distinctive spaced
    # second username "Dave Lee" as a NON-basename component, so any leak (or a
    # re-anchor slip) surfaces "Dave"/"Lee" (or "Users") in the output.
    _SECOND = {
        "drive":        r"D:\Users\Dave Lee\secret.txt",
        "env_appdata":  r"%APPDATA%\Dave Lee\secret.txt",
        "env_userprof": r"$env:USERPROFILE\Dave Lee\secret.txt",
        "unc":          r"\\fileserver\share\Dave Lee\secret.txt",
        "driveless":    r"\Users\Dave Lee\secret.txt",
        "posix":        "/Users/Dave Lee/secret.txt",
    }
    _CONNECTORS = ("comma", "semicolon", "paren", "glued", "space", "repr_list")

    @staticmethod
    def _join(first: str, second: str, connector: str) -> str:
        if connector == "space":            # control: whitespace-tempered split
            return first + " " + second
        if connector == "repr_list":        # control: repr([...]) list separator
            return "['%s', '%s']" % (first, second)
        glue = {"comma": ",", "semicolon": ";", "paren": ") (", "glued": ""}[connector]
        return first + glue + second

    def test_no_second_username_survives(self) -> None:
        for step, first in self._FIRST.items():
            for anchor, second in self._SECOND.items():
                for connector in self._CONNECTORS:
                    text = self._join(first, second, connector)
                    with self.subTest(step=step, anchor=anchor, connector=connector):
                        out = scrub_paths(text)
                        self.assertNotIn("Dave Lee", out)
                        self.assertNotIn("Dave", out)
                        self.assertNotIn("Lee", out)
                        # The second path's home root never rides along either.
                        self.assertNotIn("Users", out)

    def test_first_path_still_reduces_under_every_second_path(self) -> None:
        # The glued second path must not defeat the first path's own reduction:
        # the app-data placeholder / source marker still appears in the output.
        for anchor, second in self._SECOND.items():
            for connector in self._CONNECTORS:
                with self.subTest(anchor=anchor, connector=connector):
                    ad = self._join(self._FIRST["app_data"], second, connector)
                    self.assertIn(APP_DATA_PLACEHOLDER, scrub_paths(ad))
                    src = self._join(self._FIRST["source"], second, connector)
                    self.assertIn("zd_app", scrub_paths(src))


class JsonEscapedAcceptedTailLossTests(unittest.TestCase):
    """Width-free trim: a uniformly-doubled (JSON-escaped / ``%r``-repr) path —
    every separator width 2+ — has its tail trimmed at the first component and
    collapses to the bare placeholder / source marker.

    This is the *accepted* cost of width-freedom (see module docstring,
    "Accepted tail loss"). The previous round kept these tails by comparing a run
    to the path's own interior width, but that comparison had an irreducible
    width-2-vs-width-2 collision with a glued native-UNC root (proved by the
    closure matrix below). The structural rule drops the tail rather than risk
    the leak — never a username emitted, only diagnostic tail fidelity lost.
    """

    def test_json_escaped_app_data_tail_collapses_to_placeholder(self) -> None:
        # Every ``\\`` is doubled (width 2), so the FIRST tail run re-roots and
        # the whole tail (``logs\\sub\\zd_wrapper.log``) is dropped → <APP_DATA>.
        out = scrub_paths(
            "C:\\\\Users\\\\Jane Doe\\\\AppData\\\\Roaming\\\\ZDUltimateLegend"
            "\\\\logs\\\\sub\\\\zd_wrapper.log"
        )
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self.assertNotIn("Jane", out)

    def test_json_escaped_source_frame_tail_collapses_to_marker(self) -> None:
        # A doubled source frame keeps the marker but drops its doubled tail.
        out = scrub_paths(
            "C:\\\\Users\\\\Jane Doe\\\\proj\\\\zd_app\\\\services\\\\foo.py"
        )
        self.assertEqual(out, "zd_app")
        self.assertNotIn("Jane", out)

    def test_double_escaped_unc_second_path_collapses_leakproof(self) -> None:
        # When BOTH paths are JSON-escaped, the first path's separators are
        # ``\\`` (width 2) and the glued UNC root is ``\\\\`` (width 4) — both
        # structural re-roots, so the tail trims at the first doubled run. The
        # app-data path collapses to the bare placeholder and the second path's
        # username never rides along.
        plain = (
            r"C:\Users\Alice Doe\AppData\Roaming\ZDUltimateLegend\a.json"
            r",\\fileserver\share\Dave Lee\f.txt"
        )
        out = scrub_paths(plain.replace("\\", "\\\\"))  # JSON-escape every '\'
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)


# ---------------------------------------------------------------------------
# Round-4 class closure (base ``ecf499c``): F1/F2/F3 repros + exhaustive matrix.
#
# Path probes are built from a forward-slash TEMPLATE via ``_d`` (``/`` ->
# backslash) so the source stays readable and every backslash is produced by
# ``chr(92)`` — a raw/doubled string literal silently mis-encodes UNC/JSON probes
# and yields FALSE leaks (see memory: bash-heredoc backslash halving). ``_d`` is
# only applied to the Windows (backslash) spellings; POSIX / forward-slash-UNC
# spellings are left as literal forward slashes.
# ---------------------------------------------------------------------------
_B = chr(92)


def _d(template: str) -> str:
    """Forward-slash template -> backslash path (every separator a real chr(92))."""
    return template.replace("/", _B)


class UncFirstSecondPathLeakTests(unittest.TestCase):
    """F1 (MED, real leak): a UNC-FIRST app-data/source path (whose leading run is
    itself ``\\\\``, width 2) with a glued UNC SECOND path leaked the second
    username, because the old tail-trim compared a tail separator run to the
    *leading* run width (2 > 2 is False) and a UNC second path has no
    ``Users``/``home`` component for the home check to catch.

    Fail-on-base (``ecf499c``): the exact repro below returned
    ``<APP_DATA>/a.json/srv2/share/Dave Lee/f.txt`` — ``Dave Lee`` verbatim. The
    fix compares against the path's *interior* (non-leading) separator width, so a
    UNC-first path (interior width 1) detects the glued width-2 UNC re-root.
    """

    def test_f1_exact_repro_unc_first_glued_unc_second(self) -> None:
        first = _d("//server/share/X/ZDUltimateLegend/a.json")
        second = _d("//srv2/share/Dave Lee/f.txt")
        out = scrub_paths(first + second)            # zero-glue, as in the spec
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/a.json")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_f1_unc_first_each_connector(self) -> None:
        first = _d("//server/share/X/ZDUltimateLegend/a.json")
        second = _d("//srv2/share/Dave Lee/f.txt")
        for glue in ("", ",", ";", ") ("):
            with self.subTest(glue=repr(glue)):
                out = scrub_paths(first + glue + second)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)
                self.assertIn(f"{APP_DATA_PLACEHOLDER}/a.json", out)

    def test_f1_unc_first_source_marker(self) -> None:
        first = _d("//server/share/proj/zd_app/services/foo.py")
        second = _d("//srv2/share/Dave Lee/f.txt")
        out = scrub_paths(first + "," + second)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)
        self.assertIn("zd_app/services/foo.py", out)


class ForwardSlashUncAnchorTests(unittest.TestCase):
    """F2 (LOW, real leak): a forward-slash UNC path ``//host/share/<user>`` was
    not a recognized anchor at all, so a standalone one passed through verbatim.

    Fail-on-base (``ecf499c``): ``see //fileserver/share/Dave Lee/secret.txt now``
    was returned UNCHANGED. The new ``//`` anchor recognizes it and drops the
    username to the basename, while a URL's ``scheme://`` is still left alone.
    """

    def test_f2_exact_repro_standalone_forward_unc(self) -> None:
        text = "see //fileserver/share/Dave Lee/secret.txt now"
        out = scrub_paths(text)
        self.assertNotEqual(out, text)              # base returned it unchanged
        self.assertEqual(out, "see secret.txt now")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_forward_unc_app_data_marker(self) -> None:
        out = scrub_paths("//fileserver/share/Dave Lee/AppData/Roaming/ZDUltimateLegend/p.json")
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/p.json")
        self.assertNotIn("Dave", out)

    def test_forward_unc_bare_user_folder_is_basename(self) -> None:
        # A standalone forward-UNC path ending at a bare folder reduces to its
        # basename — the committed UNC rule (see UncEndsAtUserStandaloneTests).
        out = scrub_paths("//srv/share/Documents/report.pdf")
        self.assertEqual(out, "report.pdf")

    def test_url_scheme_not_mistaken_for_forward_unc(self) -> None:
        # ``scheme://`` must NOT anchor: the branch-local ``(?<!:)`` rejects the
        # colon. Several schemes, each left untouched.
        for url in (
            "see https://example.com/repo/path for docs",
            "fetch http://host.tld/a/b now",
            "ftp://files.example.org/pub/readme.txt",
        ):
            with self.subTest(url=url):
                self.assertEqual(scrub_paths(url), url)

    def test_extended_length_prefixes_drop_username(self) -> None:
        # ``\\?\C:\...`` / ``\\?\UNC\srv\...`` extended-length prefixes are
        # normalized away (so no stray ``?`` leads the output) and the username
        # is dropped to the file basename.
        for probe in (
            _d("//?/C:/Users/Dave Lee/Documents/f.txt"),
            _d("//?/UNC/srv/share/Dave Lee/f.txt"),
        ):
            with self.subTest(probe=probe):
                out = scrub_paths(probe)
                self.assertEqual(out, "f.txt")
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_extended_length_app_data_has_no_stray_question_mark(self) -> None:
        # Cosmetic fix: an extended-length app-data path collapses to a clean
        # ``<APP_DATA>/...`` with no leading ``?`` (was ``?<APP_DATA>/...``).
        out = scrub_paths(
            _d("//?/C:/Users/Dave Lee/AppData/Roaming/ZDUltimateLegend/p.json")
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/p.json")
        self.assertFalse(out.startswith("?"))
        self.assertNotIn("Dave", out)

    def test_device_prefix_has_no_stray_dot(self) -> None:
        # Cosmetic fix: a ``\\.\`` DEVICE prefix is normalized like ``\\?\`` (the
        # ``[?.]`` class in _EXT_LEN_PREFIX_RE), so no stray leading ``.`` leads
        # the output, and the username is still dropped.
        for probe, want in (
            (_d("//./C:/Users/Dave Lee/Documents/f.txt"), "f.txt"),
            (_d("//./C:/Users/Dave Lee"), HOME_PLACEHOLDER),
            (_d("//./C:/Users/Dave Lee/AppData/Roaming/ZDUltimateLegend/p.json"),
             f"{APP_DATA_PLACEHOLDER}/p.json"),
        ):
            with self.subTest(probe=probe):
                out = scrub_paths(probe)
                self.assertEqual(out, want)
                self.assertFalse(out.startswith("."))
                self.assertNotIn("Dave", out)


class MixedSeparatorSinglePathTests(unittest.TestCase):
    """A legit SINGLE path with a wider interior separator run (a sloppy
    ``logs//sub`` doubled-slash join, or a doubled ``\\``) is trimmed at that
    run under the width-free rule.

    The previous round (``ecf499c``) tried to PRESERVE these tails by treating a
    wider/forward run as a UNC re-root only when it introduced a real
    ``host\\share`` + content. The closure matrix proved any width comparison has
    an irreducible collision, so the width-free rule treats *any* multi-character
    run as a re-root and cuts the tail there. The lost ``//sub\\x.log`` is the
    accepted tail loss (a diagnostic nicety) — never a username, and the single
    path is not *fragmented*. A single-forward-slash tail (width 1) is still kept
    in full.
    """

    def test_f3_sloppy_double_slash_trims_at_the_doubled_run(self) -> None:
        # ``logs//sub`` — the ``//`` run is a structural re-root, so the tail is
        # cut there → <APP_DATA>/logs (accepted tail loss; ``//sub\x.log`` gone).
        probe = _d("C:/Users/A B/AppData/Roaming/ZDUltimateLegend/logs") + "//" + "sub" + _B + "x.log"
        out = scrub_paths(probe)
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs")
        self.assertNotIn("sub", out)
        self.assertNotIn("x.log", out)

    def test_doubled_backslash_interior_run_trims_tail(self) -> None:
        # The backslash twin: a single ``\\`` run inside an otherwise
        # single-separator path is width 2, a structural re-root, so the tail is
        # cut there → <APP_DATA>/logs.
        probe = _d("C:/Users/A B/AppData/Roaming/ZDUltimateLegend/logs") + _B + _B + "sub" + _B + "x.log"
        out = scrub_paths(probe)
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs")
        self.assertNotIn("sub", out)

    def test_mixed_forward_slash_single_width_tail_kept(self) -> None:
        # Single forward slashes in a backslash path are ordinary separators and
        # never trigger a trim, however many components follow.
        probe = _d("C:/Users/A B/AppData/Roaming/ZDUltimateLegend/logs") + "/sub/deep/x.log"
        out = scrub_paths(probe)
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs/sub/deep/x.log")


class SecondPathUsernameFullClosureMatrixTests(unittest.TestCase):
    r"""The round-4 class-closure matrix: NO second username survives over the
    full cross product of FIRST-path root × FIRST-path reduction × SECOND-path
    anchor × connector × {file-ending, ends-at-username}. Retained as a
    regression layer; :class:`O4bFullClosureMatrixTests` supersedes it with the
    harder mixed-escaping cells the width-free trim closes.

    Construction notes (faithful to the leak model the scrubber actually faces):
      * ``json`` first root → here the WHOLE artifact is uniformly JSON-escaped (a
        real JSON document escapes every backslash). The harder MIXED case — a
        NATIVE second path glued into a uniformly-doubled first, the
        width-2-vs-width-2 collision the round-4 width heuristic could not resolve
        — is the focus of :class:`O4bFullClosureMatrixTests`. Both run.
      * env-rooted seconds (``%APPDATA%`` / ``$env:USERPROFILE``) carry no literal
        username — the env var already encapsulates home+username — so the
        ``Dave Lee`` component is a SUBFOLDER tested with a file ending; both
        reduce to a basename that drops it.
      * ``space`` is a NON-merging control: it splits the two paths into
        independent tokens. For a UNC/forward-UNC second that ends at a bare
        folder, the standalone token then follows the committed
        ``\\server\share\file.txt`` -> basename rule (a network-share folder, NOT
        the local account home this module scrubs); that single ambiguous combo
        is asserted separately in :class:`UncEndsAtUserStandaloneTests`, so it is
        skipped here. Every other space combo (and all merging connectors) is
        covered.
    """

    _REDUCTIONS = ("appdata", "source", "home")
    _ROOTS = ("drive", "unc", "fsunc", "json", "posix")
    # (anchor, carries a literal username component?)
    _ANCHORS = (
        ("drive", True), ("env_appdata", False), ("env_userpr", False),
        ("unc", True), ("fsunc", True), ("driveless", True), ("posix", True),
    )
    _CONNECTORS = ("glue", "comma", "semi", "paren", "space", "reprlist")
    _GLUE = {"glue": "", "comma": ",", "semi": ";", "paren": ") (", "space": " "}

    @staticmethod
    def _first(root: str, reduction: str) -> str:
        body = {
            "appdata": "AppData/Roaming/ZDUltimateLegend/a.json",
            "source": "proj/zd_app/services/foo.py",
            "home": "Documents/a.json",
        }[reduction]
        if root in ("drive", "json"):
            return _d("C:/Users/Alice Doe/" + body)
        if root == "unc":
            return _d("//server/share/Users/Alice Doe/" + body)
        if root == "fsunc":
            return "//server/share/Users/Alice Doe/" + body
        if root == "posix":
            return "/home/alice/" + body
        raise AssertionError(root)

    @staticmethod
    def _second(anchor: str, ends_user: bool) -> str:
        tail = "" if ends_user else "/secret.txt"
        spelled = {
            "drive": "D:/Users/Dave Lee" + tail,
            "env_appdata": "%APPDATA%/Dave Lee/secret.txt",     # subfolder + file
            "env_userpr": "$env:USERPROFILE/Dave Lee/secret.txt",
            "unc": "//srv/share/Dave Lee" + tail,
            "fsunc": "//srv/share/Dave Lee" + tail,
            "driveless": "/Users/Dave Lee" + tail,
            "posix": "/Users/Dave Lee" + tail,
        }[anchor]
        # fsunc / posix keep forward slashes; everything else is a Windows path.
        return spelled if anchor in ("fsunc", "posix") else _d(spelled)

    def _join(self, first: str, second: str, connector: str) -> str:
        if connector == "reprlist":
            return "['%s', '%s']" % (first, second)
        return first + self._GLUE[connector] + second

    def test_no_second_username_survives_any_cell(self) -> None:
        checked = 0
        for root in self._ROOTS:
            for reduction in self._REDUCTIONS:
                first = self._first(root, reduction)
                for anchor, has_user in self._ANCHORS:
                    endings = (False, True) if has_user else (False,)
                    for ends_user in endings:
                        second = self._second(anchor, ends_user)
                        for connector in self._CONNECTORS:
                            # Skip only the committed standalone-UNC combo (see
                            # the class docstring / UncEndsAtUserStandaloneTests).
                            if (ends_user and connector == "space"
                                    and anchor in ("unc", "fsunc")):
                                continue
                            text = self._join(first, second, connector)
                            if root == "json":
                                text = text.replace(_B, _B + _B)  # uniform escape
                            out = scrub_paths(text)
                            checked += 1
                            with self.subTest(root=root, reduction=reduction,
                                              anchor=anchor,
                                              ends_user=ends_user,
                                              connector=connector):
                                self.assertNotIn("Dave Lee", out)
                                self.assertNotIn("Dave", out)
                                self.assertNotIn("Lee", out)
                                # No second-path home root rides along either.
                                self.assertNotIn("Users", out)
        # Guard against the matrix silently collapsing to a handful of cells.
        self.assertGreaterEqual(checked, 1000)

    def test_first_path_still_reduces_under_every_second(self) -> None:
        # The glued/adjacent second path must not defeat the FIRST path's own
        # reduction — proof the matrix passes by reducing correctly, not by
        # nuking the whole token.
        for root in self._ROOTS:
            for anchor, has_user in self._ANCHORS:
                endings = (False, True) if has_user else (False,)
                for ends_user in endings:
                    second = self._second(anchor, ends_user)
                    for connector in self._CONNECTORS:
                        if (ends_user and connector == "space"
                                and anchor in ("unc", "fsunc")):
                            continue
                        with self.subTest(root=root, anchor=anchor,
                                          ends_user=ends_user, connector=connector):
                            ad = self._join(self._first(root, "appdata"), second, connector)
                            src = self._join(self._first(root, "source"), second, connector)
                            if root == "json":
                                ad = ad.replace(_B, _B + _B)
                                src = src.replace(_B, _B + _B)
                            self.assertIn(APP_DATA_PLACEHOLDER, scrub_paths(ad))
                            self.assertIn("zd_app", scrub_paths(src))


class UncEndsAtUserStandaloneTests(unittest.TestCase):
    """Boundary the closure does NOT (and structurally cannot) cross: a
    *standalone* UNC / forward-UNC path that ends at a bare folder.

    ``\\\\server\\share\\file.txt`` -> ``file.txt`` is a locked preserve case
    (``test_unc_two_level_share_not_split`` / the module's "UNC two-level single
    path -> basename" contract). ``\\\\srv\\share\\Dave Lee`` is the *identical*
    3-component shape, so the same basename rule applies — there is no
    content-blind signal that keeps ``file.txt`` yet drops ``Dave Lee``.

    This is reachable only via a NON-merging connector (a whitespace gap splits
    the second path into its own standalone token); a merging connector routes it
    through the tail-trim, which DOES drop it (covered exhaustively in the matrix).
    And it is a NETWORK-share folder — not the local account home (``C:\\Users\\
    <me>``) this module exists to scrub, which ALWAYS reduces safely. Pinned here
    so the boundary is explicit, not an accidental gap.
    """

    _FIRST = property(lambda self: _d("C:/Users/Alice Doe/Documents/a.json"))

    def test_standalone_unc_ending_at_folder_follows_basename_rule(self) -> None:
        # Same rule as the locked ``\\server\share\file.txt`` -> basename case.
        self.assertEqual(scrub_paths(_d("//srv/share/Dave Lee")), "Dave Lee")
        self.assertEqual(scrub_paths("//srv/share/Dave Lee"), "Dave Lee")
        self.assertEqual(scrub_paths(_d("//server/share/file.txt")), "file.txt")

    def test_space_split_unc_with_trailing_file_is_safe(self) -> None:
        # The realistic case (a path points at a FILE): the whitespace-split
        # standalone UNC second reduces to its file basename, dropping the
        # interior username — fully safe.
        for second in (_d("//srv/share/Dave Lee/secret.txt"), "//srv/share/Dave Lee/secret.txt"):
            with self.subTest(second=second):
                out = scrub_paths(self._FIRST + " " + second)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)
                self.assertIn("secret.txt", out)

    def test_local_home_second_ending_at_user_is_safe_even_when_space_split(self) -> None:
        # The LOCAL-account-home spellings (drive / drive-less / POSIX) DO reduce
        # safely to <HOME> even when whitespace-split and ending at the username —
        # because they carry a ``Users``/``home`` root the reducer anchors on.
        for second in (_d("D:/Users/Dave Lee"), _d("/Users/Dave Lee"), "/Users/Dave Lee"):
            with self.subTest(second=second):
                out = scrub_paths(self._FIRST + " " + second)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)
                self.assertIn(HOME_PLACEHOLDER, out)


class ClassABCExactReproTests(unittest.TestCase):
    r"""The three exact repros this O4b round closes — each a REAL fail-on-base
    (``8438030``) username leak the separator-width heuristic could not stop.

    Every backslash is built from ``_B`` (= chr(92)); a raw/doubled string
    literal silently mis-encodes the width-2 interior vs native-UNC root and
    yields a FALSE pass (memory: bash-heredoc backslash halving). The
    fail-on-base output (captured by running each probe against ``8438030``) is
    noted per case; here we assert pass-on-fix — neither the first username
    (``Alice Smith``) nor the second (``Dave Lee``) survives.

    Class A — an env-var SECOND path ending at a username folder. On base it
    reduced to its leaf basename, which IS the username; PART 2's env-root
    tail-drop collapses it to a placeholder instead.

    Class B / C — a uniformly-doubled (interior width 2) FIRST path with a glued
    native-UNC SECOND path whose ``\\`` root is ALSO width 2. On base the width
    test (run width 2 is not > interior width 2) missed the re-root, so the UNC
    username rode out in the emitted tail. The width-free trim treats any
    multi-character run as a re-root and cuts there.
    """

    _A_FIRST = ("C:" + _B + "Users" + _B + "Alice Smith" + _B + "AppData" + _B
                + "Roaming" + _B + "ZDUltimateLegend" + _B + "a.json")
    # Uniformly-doubled (interior width 2) app-data first path.
    _B_FIRST = ("C:" + _B * 2 + "Users" + _B * 2 + "Alice Smith" + _B * 2
                + "AppData" + _B * 2 + "Roaming" + _B * 2 + "ZDUltimateLegend"
                + _B * 2 + "a.json")

    def _assert_no_username(self, out: str) -> None:
        for needle in ("Dave Lee", "Dave", "Lee", "Alice Smith", "Alice", "Smith"):
            self.assertNotIn(needle, out, f"username survived in {out!r}")

    def test_class_a_env_second_ends_at_username(self) -> None:
        # base 8438030 -> '<APP_DATA>/a.json,Dave Lee'   (LEAK of "Dave Lee")
        probe = self._A_FIRST + "," + "%APPDATA%" + _B + "Dave Lee"
        out = scrub_paths(probe)
        self.assertEqual(out, "<APP_DATA>/a.json,<APP_DATA>")
        self._assert_no_username(out)

    def test_class_b_glued_unc_file_tailed(self) -> None:
        # base 8438030 -> '<APP_DATA>/a.json/srv/share/Dave Lee/f.txt'   (LEAK)
        second = _B * 2 + "srv" + _B + "share" + _B + "Dave Lee" + _B + "f.txt"
        out = scrub_paths(self._B_FIRST + second)
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self._assert_no_username(out)

    def test_class_c_glued_unc_ends_at_username(self) -> None:
        # base 8438030 -> '<APP_DATA>/a.json/srv/share/Dave Lee'   (LEAK)
        second = _B * 2 + "srv" + _B + "share" + _B + "Dave Lee"
        out = scrub_paths(self._B_FIRST + second)
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self._assert_no_username(out)


class O4bFullClosureMatrixTests(unittest.TestCase):
    r"""O4b class-closure matrix — the airtight proof that NO username (second
    ``Dave Lee`` OR first ``Alice Smith``) survives over the full cross product:

        first-root  x  reduction  x  second-anchor  x  connector  x  ending

    first-root:  drive, native-UNC, forward-UNC, uniformly-doubled JSON/repr at
                 interior widths 2/3/4, drive-less ``\Users``, ``%APPDATA%`` env.
                 The JSON roots double ONLY the first path's separators and glue a
                 NATIVE second path — exactly the width-2-vs-width-2 (and
                 3/4-vs-2) collision the old width heuristic could not resolve and
                 which a uniform-escape-everything matrix never constructs.
    reduction:   app-data marker, source marker ``zd_app``, source marker
                 ``_internal``, home-rooted.
    second:      drive ``D:\``, native-UNC ``\\srv\share\<u>``, forward-UNC
                 ``//srv/share/<u>``, drive-less ``\Users\<u>``, POSIX
                 ``/Users/<u>``, env ``%APPDATA%\<u>``, ``$env:LOCALAPPDATA\<u>``.
    connector:   zero-glue, comma, semicolon, ``) (``, space, repr-list ``', '``.
    ending:      ends-at-username, file-tailed (env seconds carry no literal
                 username — ``Dave Lee`` is a subfolder — so they are file-tailed
                 only).

    The single cell the closure structurally cannot cross — a bare-folder UNC /
    forward-UNC second whitespace-split into its OWN standalone token (it then
    follows the committed ``\\srv\share\folder`` -> basename rule, a network
    share, not the local account home) — is skipped here and pinned in
    :class:`UncEndsAtUserStandaloneTests`.

    Every backslash flows through ``_d`` / ``_B`` (= chr(92)); see the round-4
    note above on why a literal mis-encodes UNC/JSON probes.
    """

    _ROOTS = ("drive", "unc", "fsunc", "json2", "json3", "json4", "driveless", "envroot")
    _REDUCTIONS = ("appdata", "source", "internal", "home")
    # (anchor, carries a literal username component?)
    _ANCHORS = (
        ("drive", True), ("unc", True), ("fsunc", True),
        ("driveless", True), ("posix", True),
        ("env_appdata", False), ("env_localapp", False),
    )
    _CONNECTORS = ("glue", "comma", "semi", "paren", "space", "reprlist")
    _GLUE = {"glue": "", "comma": ",", "semi": ";", "paren": ") (", "space": " "}
    _BODIES = {
        "appdata": "AppData/Roaming/ZDUltimateLegend/a.json",
        "source": "proj/zd_app/services/foo.py",
        "internal": "AppData/Local/Temp/_MEI/_internal/zd_app/m.py",
        "home": "Documents/a.json",
    }
    # The signature token each marker-bearing reduction must still emit.
    _MARKER = {"appdata": APP_DATA_PLACEHOLDER, "source": "zd_app", "internal": "_internal"}

    @staticmethod
    def _esc(s: str, width: int) -> str:
        """Uniformly widen every backslash run to ``width`` — a JSON/repr path."""
        return s.replace(_B, _B * width)

    def _first(self, root: str, reduction: str) -> str:
        body = self._BODIES[reduction]
        if root == "drive":
            return _d("C:/Users/Alice Smith/" + body)
        if root == "unc":
            return _d("//server/share/Users/Alice Smith/" + body)
        if root == "fsunc":
            return "//server/share/Users/Alice Smith/" + body        # forward slashes
        if root in ("json2", "json3", "json4"):
            width = {"json2": 2, "json3": 3, "json4": 4}[root]
            return self._esc(_d("C:/Users/Alice Smith/" + body), width)
        if root == "driveless":
            return _d("/Users/Alice Smith/" + body)
        if root == "envroot":
            return _d("%APPDATA%/" + body)                           # env-var first root
        raise AssertionError(root)

    @staticmethod
    def _second(anchor: str, ends_user: bool) -> str:
        tail = "" if ends_user else "/secret.txt"
        spelled = {
            "drive": "D:/Users/Dave Lee" + tail,
            "unc": "//srv/share/Dave Lee" + tail,
            "fsunc": "//srv/share/Dave Lee" + tail,
            "driveless": "/Users/Dave Lee" + tail,
            "posix": "/Users/Dave Lee" + tail,
            "env_appdata": "%APPDATA%/Dave Lee/secret.txt",         # subfolder + file
            "env_localapp": "$env:LOCALAPPDATA/Dave Lee/secret.txt",
        }[anchor]
        # fsunc / posix keep forward slashes; everything else is a Windows path.
        return spelled if anchor in ("fsunc", "posix") else _d(spelled)

    def _join(self, first: str, second: str, connector: str) -> str:
        if connector == "reprlist":
            return "['%s', '%s']" % (first, second)
        return first + self._GLUE[connector] + second

    @staticmethod
    def _skip(anchor: str, ends_user: bool, connector: str) -> bool:
        # The committed standalone-UNC boundary (see the class docstring).
        return ends_user and connector == "space" and anchor in ("unc", "fsunc")

    def test_no_username_survives_any_cell(self) -> None:
        checked = 0
        for root in self._ROOTS:
            for reduction in self._REDUCTIONS:
                first = self._first(root, reduction)
                for anchor, has_user in self._ANCHORS:
                    endings = (False, True) if has_user else (False,)
                    for ends_user in endings:
                        second = self._second(anchor, ends_user)
                        for connector in self._CONNECTORS:
                            if self._skip(anchor, ends_user, connector):
                                continue
                            out = scrub_paths(self._join(first, second, connector))
                            checked += 1
                            with self.subTest(root=root, reduction=reduction,
                                              anchor=anchor, ends_user=ends_user,
                                              connector=connector):
                                for needle in ("Dave Lee", "Dave", "Lee",
                                               "Alice Smith", "Alice", "Smith",
                                               "Users"):
                                    self.assertNotIn(needle, out)
        # Guard against the matrix silently collapsing to a handful of cells.
        self.assertGreaterEqual(checked, 2000)

    def test_first_path_marker_survives_every_cell(self) -> None:
        # The glued/adjacent second path must not defeat the FIRST path's own
        # reduction: each marker-bearing reduction still emits its signature
        # token — proof the matrix passes by REDUCING correctly, not by nuking
        # the whole token to empty.
        for root in self._ROOTS:
            for reduction in ("appdata", "source", "internal"):
                first = self._first(root, reduction)
                want = self._MARKER[reduction]
                for anchor, has_user in self._ANCHORS:
                    endings = (False, True) if has_user else (False,)
                    for ends_user in endings:
                        second = self._second(anchor, ends_user)
                        for connector in self._CONNECTORS:
                            if self._skip(anchor, ends_user, connector):
                                continue
                            with self.subTest(root=root, reduction=reduction,
                                              anchor=anchor, ends_user=ends_user,
                                              connector=connector):
                                out = scrub_paths(self._join(first, second, connector))
                                self.assertIn(want, out)


class TrailingDotSpaceHomeRootTests(unittest.TestCase):
    r"""Trailing-dot/space home-root bypass (base ``4f5939b``): Windows silently
    coalesces a path component's trailing dots and spaces, so ``C:\Users.\<name>``
    / ``\Users \<name>`` / ``/Users.../<name>`` all resolve to the REAL home —
    yet the scrubber matched the ``Users``/``home`` root by EXACT lowercase
    string (``comp.lower() in _HOME_ROOTS``) and the drive-less/POSIX anchor
    required the root word *immediately* before its separator. Both missed the
    decorated spellings, so the account username leaked.

    Three fail-on-base sub-classes, each pinned below:
      * drive-rooted (``C:\Users.\Dave Lee``) — anchored via ``C:\`` but step-4's
        exact match skipped the username drop, so the basename WAS the username
        (base -> ``Dave Lee``);
      * drive-less / POSIX (``\Users.\Dave Lee`` / ``/Users./Dave Lee``) — never
        anchored, so the whole path passed through verbatim (base -> unchanged);
      * a decorated ``Users.`` riding in a merged-token TAIL (base leaked the
        second path's username, e.g. ``Bob Roe``).

    The fix mirrors the OS coalescing: :func:`~zd_app.services.path_scrub`'s
    shared ``_is_home_root`` accessor rstrips ``". "`` before the ``_HOME_ROOTS``
    test (used by both the reducer's home step and the tail-trim), and the
    drive-less/POSIX anchor tolerates ``[. ]*`` before its closing separator.

    Every backslash flows through ``_B`` (= chr(92)) / ``_d``; a raw/doubled
    literal silently mis-encodes path probes (see the round-4 note above).
    """

    _USER = "Dave Lee"

    def test_drive_rooted_trailing_dot_exact_repro(self) -> None:
        # base 4f5939b -> 'Dave Lee' (anchors via C:\, but step-4 'users.' !=
        # 'users' skips the username drop, so the basename IS the username).
        out = scrub_paths(_d("C:/Users./" + self._USER))
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_driveless_trailing_dot_exact_repro(self) -> None:
        # base 4f5939b -> returned UNCHANGED ('\Users.\Dave Lee' verbatim): the
        # rooted-home anchor needs Users/home immediately before its separator.
        probe = _d("/Users./" + self._USER)
        out = scrub_paths(probe)
        self.assertNotEqual(out, probe)              # base returned it unchanged
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_posix_trailing_dot_exact_repro(self) -> None:
        # base 4f5939b -> '/Users./Dave Lee' verbatim (same anchor miss).
        probe = "/Users./" + self._USER
        out = scrub_paths(probe)
        self.assertNotEqual(out, probe)
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_home_root_word_and_mixed_case_decorated(self) -> None:
        # The ``home`` root word and case-insensitivity both still hold with
        # decoration (rstrip(". ") runs after .lower()).
        for root in ("home.", "HOME ", "Home...", "users.", "UsErS ."):
            with self.subTest(root=root):
                out = scrub_paths(_d("C:/" + root + "/" + self._USER))
                self.assertEqual(out, HOME_PLACEHOLDER)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_mixed_slash_decorated_root(self) -> None:
        # The decorated anchor tolerates either slash direction on each side.
        for probe in (
            _B + "Users./" + self._USER,             # back-slash in, forward out
            "/Users." + _B + self._USER,             # forward in, back-slash out
        ):
            with self.subTest(probe=probe):
                out = scrub_paths(probe)
                self.assertEqual(out, HOME_PLACEHOLDER)
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_file_tailed_decorated_reduces_to_basename(self) -> None:
        # The leak needs the username as the LEAF; a file tail reduces to the
        # file basename with the (decorated-root) username dropped — no leak.
        for probe in (
            _d("C:/Users./" + self._USER + "/secret.txt"),
            _d("/Users /" + self._USER + "/secret.txt"),
            "/Users./" + self._USER + "/secret.txt",
        ):
            with self.subTest(probe=probe):
                out = scrub_paths(probe)
                self.assertEqual(out, "secret.txt")
                self.assertNotIn("Dave", out)
                self.assertNotIn("Lee", out)

    def test_clean_controls_unchanged(self) -> None:
        # The undecorated forms must behave EXACTLY as before the fix.
        self.assertEqual(scrub_paths(_d("C:/Users/Dave Lee")), HOME_PLACEHOLDER)
        self.assertEqual(scrub_paths(_d("C:/Users/Dave Lee/secret.txt")), "secret.txt")
        self.assertEqual(scrub_paths(_d("/Users/Dave Lee")), HOME_PLACEHOLDER)
        self.assertEqual(scrub_paths("/Users/Dave Lee"), HOME_PLACEHOLDER)

    def test_decorated_users_in_merged_tail_is_trimmed(self) -> None:
        # The _tail_before_reanchor side of the fix: a decorated drive-less home
        # path glued onto an app-data tail must be trimmed at the (decorated)
        # 'Users.' re-root. base 4f5939b -> '<APP_DATA>/a.json/Users./Bob Roe/
        # b.json' (LEAK of 'Bob Roe'); fix -> '<APP_DATA>/a.json'.
        ad = _d("C:/Users/Alice Doe/AppData/Roaming/ZDUltimateLegend/a.json")
        out = scrub_paths(ad + _d("/Users./Bob Roe/b.json"))
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/a.json")
        self.assertNotIn("Bob", out)
        self.assertNotIn("Roe", out)

    def test_decorated_users_in_source_tail_is_trimmed(self) -> None:
        # Same, after a source marker (space-decorated root this time).
        src = _d("C:/Users/Alice Doe/proj/zd_app/services/foo.py")
        out = scrub_paths(src + _d("/Users /Bob Roe/b.json"))
        self.assertEqual(out, "zd_app/services/foo.py")
        self.assertNotIn("Bob", out)
        self.assertNotIn("Roe", out)

    def test_decorated_whitespace_split_multipath_no_regression(self) -> None:
        # CAUTION case (the [. ]* anchor carries a space): the body's whitespace
        # tempering must still split a decorated second home path into its own
        # token — each reduces, the prose connector is preserved, and nothing
        # over-matches. base 4f5939b leaked 'Bob Lee' (the 2nd path's username).
        p = _d("C:/Users/Alice Doe/AppData/Roaming/ZDUltimateLegend/p.json")
        line = "copy " + p + " to " + _d("/Users./Bob Lee/q.json")
        out = scrub_paths(line)
        self.assertEqual(out, "copy <APP_DATA>/p.json to q.json")
        for needle in ("Alice", "Bob", "Lee", "Users"):
            self.assertNotIn(needle, out)

    def test_undecorated_whitespace_split_multipath_unchanged(self) -> None:
        # The plain (undecorated) twin pins that the anchor change did NOT alter
        # the established whitespace-split multi-path output.
        p = _d("C:/Users/Alice Doe/AppData/Roaming/ZDUltimateLegend/p.json")
        line = "copy " + p + " to " + _d("C:/Users/Alice Doe/Documents/export.json")
        out = scrub_paths(line)
        self.assertEqual(out, "copy <APP_DATA>/p.json to export.json")
        self.assertNotIn("Alice", out)

    def test_interior_decorated_users_single_path_no_fragment(self) -> None:
        # Over-match guard: a decorated interior 'Users.' in a SINGLE path must
        # not fragment it; it reduces to the file basename with the post-Users
        # component dropped (same as the undecorated interior case).
        out = scrub_paths(_d("C:/data/Users./Bob/file.txt"))
        self.assertEqual(out, "file.txt")
        self.assertNotIn("Bob", out)


class TrailingDotSpaceClosureMatrixTests(unittest.TestCase):
    r"""Exhaustive trailing-dot/space matrix: NO username survives over the full
    cross product of root × decoration × form × slash × ending, and every clean
    reduction is exact (bare-home -> ``<HOME>``; file-tailed -> the basename).

        root        Users / users / UsErS / home / HOME / Home  (case-insensitive)
        decoration  '.'  '..'  '...'  ' '  ' .'  '. '            (all coalesce off)
        form         drive  (C:\)  |  drive-less  (\)
        slash        backslash (chr 92)  |  forward (/)
        ending       bare-home (username is the leaf)  |  file-tailed

    6 × 6 × 2 × 2 × 2 = 288 cells. Each decoration is built only from ``.``/`` ``
    so it always falls inside the anchor's ``[. ]*`` and the accessor's
    ``rstrip(". ")`` — proving the whole class closed, not just the spot repros.
    Backslashes come from ``_B`` (= chr(92)); see the round-4 note above.
    """

    _ROOTS = ("Users", "users", "UsErS", "home", "HOME", "Home")
    _DECOS = (".", "..", "...", " ", " .", ". ")
    # (kind, separator): drive vs drive-less × backslash vs forward slash.
    _FORMS = (("drive", _B), ("drive", "/"), ("driveless", _B), ("driveless", "/"))
    _USER = "Dave Lee"

    @classmethod
    def _build(cls, root: str, deco: str, kind: str, sep: str,
               file_tailed: bool) -> str:
        comps = [root + deco, cls._USER]
        if file_tailed:
            comps.append("secret.txt")
        body = sep.join(comps)
        return ("C:" + sep + body) if kind == "drive" else (sep + body)

    def test_no_username_survives_any_cell(self) -> None:
        checked = 0
        for root in self._ROOTS:
            for deco in self._DECOS:
                for kind, sep in self._FORMS:
                    for file_tailed in (False, True):
                        probe = self._build(root, deco, kind, sep, file_tailed)
                        out = scrub_paths(probe)
                        checked += 1
                        with self.subTest(root=root, deco=repr(deco), kind=kind,
                                          slash=("fwd" if sep == "/" else "bslash"),
                                          file_tailed=file_tailed):
                            self.assertNotIn("Dave Lee", out)
                            self.assertNotIn("Dave", out)
                            self.assertNotIn("Lee", out)
                            self.assertNotIn("Users", out)
                            # Exact reduced form, not just absence of the leak.
                            self.assertEqual(
                                out,
                                "secret.txt" if file_tailed else HOME_PLACEHOLDER,
                            )
        # Guard against the matrix silently collapsing to a handful of cells.
        self.assertGreaterEqual(checked, 250)


class InteriorDotTraversalHomeRootTests(unittest.TestCase):
    r"""The two canonicalization siblings this round closes (base ``3316462``):
    an interior ``.`` (current-dir) and ``..`` (parent) component that Windows
    resolves *before* it opens the path, so a decorated home spelling still
    reaches the REAL home and the resolved component is still the account
    username.

    Filesystem-verified on a live box (so the leaks are genuine, not a probe
    artifact): ``os.path.isdir(r'C:\Users\.\<me>')`` and
    ``os.path.isdir(r'C:\Users\<me>\..\<me>')`` are both True — Windows collapses
    the ``.``/``..`` to the real home.

    Fail-on-base: a split-on-separators reducer took the literal post-root
    component (the ``.``, or the pre-``..`` name) as the "username" and re-emitted
    the RESOLVED username as the basename. The exact base output is noted per
    case; here we assert pass-on-fix.

    Every backslash flows through ``_B`` (= chr(92)) / ``_d``; a raw/doubled
    literal silently mis-encodes path probes (see the round-4 note above).
    """

    def test_interior_dot_after_root_drive_backslash(self) -> None:
        # base 3316462 -> 'Dave Lee' (the '.' was dropped as the "username",
        # leaving the RESOLVED username 'Dave Lee' as the basename).
        out = scrub_paths(_d("C:/Users/./Dave Lee"))
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_interior_dot_after_root_forward_slash_twin(self) -> None:
        # base 3316462 -> 'Dave Lee'. The forward-slash twin (no _d).
        out = scrub_paths("C:/Users/./Dave Lee")
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_sibling_traversal_dotdot(self) -> None:
        # base 3316462 -> 'Eve Ng'. 'Users\Dave Lee\..\Eve Ng' resolves to
        # 'Users\Eve Ng' — the leaf IS the username. Collapses to <HOME>.
        out = scrub_paths(_d("C:/Users/Dave Lee/../Eve Ng"))
        self.assertEqual(out, HOME_PLACEHOLDER)
        for needle in ("Dave", "Lee", "Eve", "Ng"):
            self.assertNotIn(needle, out)

    def test_traversal_to_leaf_dotdot(self) -> None:
        # base 3316462 -> 'Bob'. 'Users\Alice\..\Bob' resolves to 'Users\Bob'.
        out = scrub_paths(_d("C:/Users/Alice/../Bob"))
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Alice", out)
        self.assertNotIn("Bob", out)

    def test_legit_interior_dot_keeps_file_basename(self) -> None:
        # The control: an interior '.' in the TAIL (after the username) is a
        # no-op — the real username is still dropped, but the legit file basename
        # SURVIVES, not collapsed. (Pass-on-base control that must STAY green.)
        out = scrub_paths(_d("C:/Users/humphrey/./app.log"))
        self.assertEqual(out, "app.log")
        self.assertNotIn("humphrey", out)

    def test_dotdot_immediately_after_root_to_leaf(self) -> None:
        # base 3316462 -> 'Bob'. 'Users\..\Bob' resolves to 'C:\Bob' (not even a
        # home), but a '..' right after the root still collapses to the safe
        # floor <HOME> — the username-locating must NOT mistake '..' for the name.
        out = scrub_paths(_d("C:/Users/../Bob"))
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Bob", out)

    def test_dotdot_before_root_still_finds_root(self) -> None:
        # A '..' BEFORE the home root needs no special handling: the root is
        # still found and its next component (the username) dropped; the file
        # basename survives. 'C:\foo\..\Users\Dave Lee\secret.txt' resolves to
        # 'C:\Users\Dave Lee\secret.txt'.
        out = scrub_paths(_d("C:/foo/../Users/Dave Lee/secret.txt"))
        self.assertEqual(out, "secret.txt")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_file_tailed_interior_dot_drops_username_keeps_file(self) -> None:
        # Interior '.' right after the root, file-tailed: the '.' is skipped, the
        # RESOLVED username dropped, the file basename kept.
        out = scrub_paths(_d("C:/Users/./Dave Lee/secret.txt"))
        self.assertEqual(out, "secret.txt")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_decorated_root_plus_interior_dot(self) -> None:
        # The _is_home_root + canonicalization combo: a trailing-dot-decorated
        # root AND an interior '.' (``C:\Users.\.\Dave Lee``) still drops the
        # resolved username. base 3316462 -> 'Dave Lee'.
        out = scrub_paths(_d("C:/Users./././Dave Lee"))
        self.assertEqual(out, HOME_PLACEHOLDER)
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)


class TraversalInMarkerTailTrimTests(unittest.TestCase):
    r"""The ``_tail_before_reanchor`` side of the fix: a ``..`` riding inside an
    app-data / app-source tail can walk back *up* out of the marker dir to a home
    whose leaf is a username, so it is trimmed at the first ``..`` there too.

    Fail-on-base (``3316462``): ``..`` was not a tail re-root tell, so step 1/2
    emitted the doubled-back tail verbatim — e.g.
    ``<APP_DATA>/../../Eve Ng`` for the app-data case (a leak of ``Eve Ng``, which
    ``...\ZDUltimateLegend\..\..\Eve Ng`` can resolve onto a real ``Users`` leaf).
    The home-rooted branch (step 4) collapses to ``<HOME>`` on any post-root
    ``..`` and never reaches here with one; this covers the MARKER tails.
    """

    def test_dotdot_in_app_data_tail_trimmed(self) -> None:
        # base 3316462 -> '<APP_DATA>/../../Eve Ng' (LEAK). Trimmed at the first
        # '..' -> bare <APP_DATA>.
        out = scrub_paths(
            _d("C:/Users/humphrey/AppData/Roaming/ZDUltimateLegend/../../Eve Ng")
        )
        self.assertEqual(out, APP_DATA_PLACEHOLDER)
        self.assertNotIn("Eve", out)
        self.assertNotIn("Ng", out)

    def test_dotdot_in_source_tail_trimmed(self) -> None:
        # base 3316462 -> 'zd_app/services/../../Dave Lee' (LEAK). The tail is cut
        # at the FIRST '..', so the legit pre-'..' component 'services' is kept
        # and the username 'Dave Lee' is dropped -> 'zd_app/services'.
        out = scrub_paths(
            _d("C:/Users/humphrey/proj/zd_app/services/../../Dave Lee")
        )
        self.assertEqual(out, "zd_app/services")
        self.assertNotIn("Dave", out)
        self.assertNotIn("Lee", out)

    def test_legit_app_data_tail_without_dotdot_preserved(self) -> None:
        # Control: a real single-separator app-data tail carries no '..' and is
        # preserved in full (the '..' trim must not over-fire).
        out = scrub_paths(
            _d("C:/Users/humphrey/AppData/Roaming/ZDUltimateLegend/logs/sub/x.log")
        )
        self.assertEqual(out, f"{APP_DATA_PLACEHOLDER}/logs/sub/x.log")


class InteriorDotTraversalClosureMatrixTests(unittest.TestCase):
    r"""Exhaustive interior ``.`` / ``..`` matrix: NO username (``Dave Lee`` /
    ``Eve Ng`` / ``Bob``) survives over the full cross product of root-form ×
    slash × scenario × ending, and every clean reduction is EXACT (a ``..`` —
    anywhere after the root — and a bare-home ``.`` collapse to ``<HOME>``; an
    interior ``.`` that leaves a real file keeps the file basename).

        root-form   drive (``C:\``)  |  drive-less (``\``)    [POSIX = driveless + '/']
        slash       backslash (chr 92)  |  forward (``/``)
        scenario    '.' after root | '.' in tail | '..' after root |
                    '..' reposition-to-leaf | decorated-root + '.'
        ending      leaf-username  |  file-tailed

    2 forms × 2 slashes × 5 scenarios × 2 endings = 40 cells. Each backslash
    comes from ``_B`` (= chr(92)); a raw/doubled literal mis-encodes the probes
    (see the round-4 note above).
    """

    _SCENARIOS = (
        "dot_after_root", "dot_in_tail", "dotdot_after_root",
        "dotdot_reposition", "decorated_traversal",
    )
    _FORMS = (("drive", _B), ("drive", "/"), ("driveless", _B), ("driveless", "/"))

    @staticmethod
    def _layout(scenario: str, file_tailed: bool):
        """(root_decoration, components-after-root, expected-output)."""
        tail = ["secret.txt"] if file_tailed else []
        file_or_home = "secret.txt" if file_tailed else HOME_PLACEHOLDER
        if scenario == "dot_after_root":
            # interior '.' immediately after the root; leaf is the username.
            return "", [".", "Dave Lee"] + tail, file_or_home
        if scenario == "dot_in_tail":
            # '.' is a no-op in the tail AFTER the username.
            if file_tailed:
                return "", ["Dave Lee", ".", "secret.txt"], "secret.txt"
            return "", ["Dave Lee", "."], HOME_PLACEHOLDER     # bare home
        if scenario == "dotdot_after_root":
            # '..' right after the root -> <HOME> regardless of ending.
            return "", ["..", "Bob"] + tail, HOME_PLACEHOLDER
        if scenario == "dotdot_reposition":
            # '..' repositions the username to the leaf -> <HOME> either ending.
            return "", ["Dave Lee", "..", "Eve Ng"] + tail, HOME_PLACEHOLDER
        if scenario == "decorated_traversal":
            # trailing-dot-decorated root + interior '.'; leaf is the username.
            return ".", [".", "Dave Lee"] + tail, file_or_home
        raise AssertionError(scenario)

    def _build(self, form: str, sep: str, scenario: str, file_tailed: bool):
        deco, comps, expected = self._layout(scenario, file_tailed)
        body = sep.join(["Users" + deco] + comps)
        path = ("C:" + sep + body) if form == "drive" else (sep + body)
        return path, expected

    def test_no_username_survives_any_cell(self) -> None:
        checked = 0
        for form, sep in self._FORMS:
            for scenario in self._SCENARIOS:
                for file_tailed in (False, True):
                    probe, expected = self._build(form, sep, scenario, file_tailed)
                    out = scrub_paths(probe)
                    checked += 1
                    with self.subTest(form=form,
                                      slash=("fwd" if sep == "/" else "bslash"),
                                      scenario=scenario, file_tailed=file_tailed):
                        for needle in ("Dave Lee", "Eve Ng", "Bob",
                                       "Dave", "Lee", "Eve", "Ng", "Users"):
                            self.assertNotIn(needle, out)
                        # Exact reduced form, not just absence of the leak.
                        self.assertEqual(out, expected)
        # Guard against the matrix silently collapsing to a handful of cells.
        self.assertGreaterEqual(checked, 40)


if __name__ == "__main__":
    unittest.main()
