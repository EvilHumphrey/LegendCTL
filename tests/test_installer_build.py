"""Tests for the Inno Setup installer script.

These are static checks — they parse the .iss file as text and assert
required directives are present. The actual ISCC.exe compile happens via
``tools/build_release.ps1`` and requires Inno Setup on the build machine;
that integration is operator-verifiable manually.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ISS_PATH = Path(__file__).resolve().parent.parent / "tools" / "installer" / "inno_setup_zd_wrapper.iss"


def _read_iss() -> str:
    return _ISS_PATH.read_text(encoding="utf-8")


class InnoScriptSyntaxTests(unittest.TestCase):
    """Basic structural assertions on the .iss file."""

    def test_iss_file_exists(self) -> None:
        self.assertTrue(_ISS_PATH.is_file(), f"missing {_ISS_PATH}")

    def test_required_sections_present(self) -> None:
        body = _read_iss()
        for section in ("[Setup]", "[Files]", "[Icons]", "[Tasks]", "[Run]"):
            self.assertIn(section, body, f"required section {section} missing")

    def test_admin_install_for_acl_hardening(self) -> None:
        """Install requires admin so {app} inherits admin-only-write ACLs
        (blocks DLL planting / binary tamper)."""
        body = _read_iss()
        self.assertRegex(
            body,
            re.compile(r"^PrivilegesRequired\s*=\s*admin\b", re.MULTILINE),
            "PrivilegesRequired=admin is the hardened contract",
        )
        self.assertNotRegex(
            body,
            re.compile(r"^PrivilegesRequired\s*=\s*lowest\b", re.MULTILINE),
        )

    def test_admin_requirement_not_overridable_to_user_writable(self) -> None:
        """The admin requirement must not be downgradable via command line —
        that would reintroduce the user-writable install."""
        body = _read_iss()
        self.assertNotRegex(
            body,
            re.compile(r"^PrivilegesRequiredOverridesAllowed\s*=", re.MULTILINE),
        )

    def test_default_install_location_is_program_files(self) -> None:
        """Install under Program Files (admin-only-write), not the
        user-writable {localappdata}."""
        body = _read_iss()
        self.assertIn(r"DefaultDirName={autopf}\ZDUltimateLegend", body)
        self.assertNotIn(r"DefaultDirName={localappdata}", body)

    def test_default_group_name_is_branded(self) -> None:
        body = _read_iss()
        self.assertRegex(
            body,
            re.compile(r"^DefaultGroupName\s*=\s*ZD Ultimate Legend Wrapper\s*$", re.MULTILINE),
        )

    def test_app_id_matches_version_app_id(self) -> None:
        """AppId must align with __app_id__ in zd_app/version.py for uninstall correlation."""
        from zd_app.version import __app_id__

        body = _read_iss()
        self.assertIn(f"{{{{{__app_id__}}}", body)

    def test_architectures_use_non_deprecated_identifier(self) -> None:
        """Inno 6.7.1 warns on bare x64; must use x64compatible per docs."""
        body = _read_iss()
        self.assertRegex(
            body,
            re.compile(r"^ArchitecturesAllowed\s*=\s*x64compatible\b", re.MULTILINE),
        )
        self.assertRegex(
            body,
            re.compile(r"^ArchitecturesInstallIn64BitMode\s*=\s*x64compatible\b", re.MULTILINE),
        )

    def test_version_directive_uses_zdul_version_env_var(self) -> None:
        """Version is injected by build_release.ps1 via $env:ZDUL_VERSION."""
        body = _read_iss()
        # Used in AppVersion, OutputBaseFilename, and the [Files] Source line.
        self.assertGreaterEqual(
            len(re.findall(r'GetEnv\("ZDUL_VERSION"\)', body)),
            3,
            "ZDUL_VERSION must be referenced in AppVersion, OutputBaseFilename, and [Files] Source",
        )

    def test_no_signtool_directives_yet(self) -> None:
        """The installer is unsigned; SignTool directives belong to a later release."""
        body = _read_iss()
        # Only assert the actual directive forms; the header comment block
        # may legitimately mention "SignTool" while documenting the deferral.
        self.assertNotRegex(body, re.compile(r"^SignTool\s*=", re.MULTILINE))
        self.assertNotRegex(body, re.compile(r"^SignedUninstaller\s*=", re.MULTILINE))
        self.assertNotIn("[Signing]", body)

    def test_no_license_file_directive_yet(self) -> None:
        """Installer license screen is deferred (separate from the MIT project
        license); LicenseFile= must stay absent until the operator opts in."""
        body = _read_iss()
        self.assertNotRegex(
            body,
            re.compile(r"^LicenseFile\s*=", re.MULTILINE),
            "LicenseFile= must wait until operator picks a license",
        )

    def test_iss_has_zdul_version_guard(self) -> None:
        """Direct ISCC invocation without ZDUL_VERSION should fail loudly, not silently."""
        text = _read_iss()
        self.assertIn("#if", text)
        self.assertIn("ZDUL_VERSION", text)
        self.assertIn("#error", text)

    def test_desktop_icon_task_is_unchecked_by_default(self) -> None:
        """Desktop shortcut is opt-in (Flags: unchecked)."""
        body = _read_iss()
        # The desktopicon Name: ... and Flags: unchecked may span multiple
        # lines via the \-continuation; DOTALL lets . cross newlines.
        self.assertRegex(
            body,
            re.compile(r'Name:\s*"desktopicon".*Flags:\s*unchecked', re.DOTALL),
            "[Tasks] desktopicon must be unchecked by default",
        )

    def test_h2_implemented_no_todo_and_launch_deelevated(self) -> None:
        """The installer uses Program Files + admin; the gating TODO is gone and
        the postinstall launch drops elevation (runasoriginaluser) so the wrapper
        doesn't run as admin after an elevated install."""
        body = _read_iss()
        self.assertNotIn("TODO(security-H2)", body)
        self.assertRegex(body, re.compile(r"^\s*Flags:[^\n]*runasoriginaluser", re.MULTILINE))


class BuildReleaseScriptTests(unittest.TestCase):
    """Light static checks on the build_release.ps1 native-command changes."""

    @classmethod
    def setUpClass(cls) -> None:
        ps1_path = Path(__file__).resolve().parent.parent / "tools" / "build_release.ps1"
        cls.body = ps1_path.read_text(encoding="utf-8")

    def test_iscc_path_probes_both_program_files_locations(self) -> None:
        self.assertIn(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", self.body)
        self.assertIn(r"C:\Program Files\Inno Setup 6\ISCC.exe", self.body)

    def test_soft_fail_when_iscc_absent(self) -> None:
        """Missing Inno must Write-Warning, not Write-Error — ZIP build must still succeed."""
        self.assertRegex(self.body, r"Write-Warning\s+\"Inno Setup compiler.*not found")

    def test_passes_zdul_version_env_var(self) -> None:
        self.assertIn("$env:ZDUL_VERSION = $version", self.body)

    def test_invokes_iss_script_at_expected_path(self) -> None:
        self.assertIn(r"tools\installer\inno_setup_zd_wrapper.iss", self.body)

    def test_installs_pinned_build_requirements_not_floating_pyinstaller(self) -> None:
        """Build installs the pinned requirements-build.txt, never floats/upgrades PyInstaller."""
        self.assertIn("-r requirements-build.txt", self.body)
        self.assertNotIn("--upgrade pyinstaller", self.body)

    def test_writes_sha256sums_artifact(self) -> None:
        """Distribution-trust: ZIP (+ installer) hashes are persisted to SHA256SUMS.txt."""
        self.assertIn("SHA256SUMS.txt", self.body)
        self.assertRegex(self.body, r"Set-Content\s+-Path\s+\$sumsPath")

    def test_patches_build_date_into_version(self) -> None:
        """About screen shows a build date; build_release.ps1 must inject __build_date__."""
        self.assertIn("__build_date__", self.body)
        self.assertRegex(self.body, r"\$buildDate\s*=\s*Get-Date")

    def test_version_py_write_uses_no_newline_flag(self) -> None:
        """Regression guard: every
        Set-Content call that targets zd_app/version.py must use -NoNewline.

        Get-Content -Raw preserves the source file's trailing newline in the
        captured string; Set-Content then appends its own trailing newline by
        default. Without -NoNewline the round-trip leaves the file one `\\r\\n`
        longer than it started, and the drift accumulates one trailing blank
        line per build. Both the patched-write and the finally-block rollback
        must opt out via -NoNewline.
        """
        matches = list(
            re.finditer(
                r'^[^\r\n#]*Set-Content\s+["\']?zd_app\\version\.py["\']?[^\r\n]*$',
                self.body,
                re.MULTILINE,
            )
        )
        # The script has two Set-Content calls on version.py: the patched
        # write (post-regex __build_commit__/__build_date__ injection) and the
        # finally-block rollback. Both must exist and both must be -NoNewline.
        self.assertGreaterEqual(
            len(matches),
            2,
            "expected at least 2 Set-Content calls on zd_app\\version.py "
            "(patched-write + rollback); regex matched: "
            + repr([m.group(0) for m in matches]),
        )
        for match in matches:
            line = match.group(0)
            self.assertIn(
                "-NoNewline",
                line,
                f"Set-Content on zd_app\\version.py without -NoNewline (trailing-newline regression): {line!r}",
            )


class BuildReleaseNativeCommandHardeningTests(unittest.TestCase):
    """Build-release native-command hardening: every native-command invocation in
    build_release.ps1 must route through Invoke-NativeCommand so PS 5.1's strict
    ErrorActionPreference="Stop" doesn't wrap pip/PyInstaller/ISCC stderr as a
    NativeCommandError and abort the script before $LASTEXITCODE can be checked."""

    @classmethod
    def setUpClass(cls) -> None:
        ps1_path = Path(__file__).resolve().parent.parent / "tools" / "build_release.ps1"
        cls.body = ps1_path.read_text(encoding="utf-8")

    def test_script_defines_invoke_native_command_helper(self) -> None:
        """Helper must exist with the documented lifecycle: param($Label,$ScriptBlock),
        set ErrorActionPreference='Continue', invoke, check $LASTEXITCODE, throw on
        non-zero, restore prior preference in finally."""
        body = self.body
        self.assertRegex(body, re.compile(r"^function\s+Invoke-NativeCommand\b", re.MULTILINE))
        self.assertRegex(body, r"\[Parameter\(Mandatory\)\]\[string\]\$Label\b")
        self.assertRegex(body, r"\[Parameter\(Mandatory\)\]\[scriptblock\]\$ScriptBlock\b")
        self.assertRegex(
            body,
            r"\$prev\s*=\s*\$ErrorActionPreference[\s\S]{0,200}\$ErrorActionPreference\s*=\s*'Continue'",
            "helper must capture prior preference then override to 'Continue'",
        )
        self.assertRegex(
            body,
            r"if\s*\(\s*\$LASTEXITCODE\s*-ne\s*0\s*\)\s*\{\s*throw\b",
            "helper must throw on non-zero $LASTEXITCODE",
        )
        self.assertRegex(
            body,
            r"\}\s*finally\s*\{\s*\$ErrorActionPreference\s*=\s*\$prev",
            "helper must restore prior preference in finally",
        )

    def test_pip_invocation_routes_through_helper(self) -> None:
        """pip install (requirements-build.txt) must run inside Invoke-NativeCommand."""
        self.assertRegex(
            self.body,
            re.compile(
                r'Invoke-NativeCommand\s+-Label\s+"[^"]*pip install[^"]*"\s+-ScriptBlock\s*\{[^}]*\$python\s+-m\s+pip\s+install[^}]*\}',
                re.IGNORECASE,
            ),
            "pip install must be wrapped by Invoke-NativeCommand with a pip-labelled -Label and a -ScriptBlock that runs `$python -m pip install`",
        )

    def test_pyinstaller_invocation_routes_through_helper(self) -> None:
        """PyInstaller must run inside Invoke-NativeCommand."""
        self.assertRegex(
            self.body,
            re.compile(
                r'Invoke-NativeCommand\s+-Label\s+"[^"]*PyInstaller[^"]*"\s+-ScriptBlock\s*\{[^}]*\$python\s+-m\s+PyInstaller[^}]*\}',
            ),
            "PyInstaller must be wrapped by Invoke-NativeCommand with a PyInstaller-labelled -Label and a -ScriptBlock that runs `$python -m PyInstaller`",
        )

    def test_inno_setup_invocation_routes_through_helper(self) -> None:
        """Inno Setup (ISCC) must run inside Invoke-NativeCommand. The prior pattern
        had its own `if ($LASTEXITCODE -ne 0) { Write-Error ... }` guard; the helper
        consolidates the throw-on-non-zero so the success path falls through directly."""
        self.assertRegex(
            self.body,
            re.compile(
                r'Invoke-NativeCommand\s+-Label\s+"[^"]*Inno Setup[^"]*"\s+-ScriptBlock\s*\{[^}]*\$inno\b[^}]*\}',
                re.IGNORECASE,
            ),
            "Inno Setup compile must be wrapped by Invoke-NativeCommand with an Inno-labelled -Label and a -ScriptBlock that runs `$inno`",
        )
        # The prior raw `if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup compile failed` pattern must be gone — the helper throws.
        self.assertNotRegex(
            self.body,
            re.compile(r'Write-Error\s+"Inno Setup compile failed'),
            "the helper throws on non-zero; the prior Write-Error pattern must be removed (otherwise both fire)",
        )

    def test_no_raw_native_call_without_helper(self) -> None:
        """Regression guard: every `& $native_exe` invocation must route through
        Invoke-NativeCommand on the same line. Allowlist: `& $ScriptBlock` (the
        helper's own dispatch). Other `& <token>` forms aren't matched —
        `& git rev-parse ...` (no $variable) and `& (Join-Path ...)` (scriptblock
        invocation, not native-exe variable) are legitimate and outside the scope
        of the PS 5.1 NativeCommandError bug.

        Block comments (`<# ... #>`, which include comment-based help) are stripped
        before scanning so example invocations in docstrings don't trip the guard.
        Line-by-line accounting after stripping preserves line numbers via newline
        replacement so any failure message still points at the real source line.
        """
        allowlist = {"ScriptBlock"}

        def _strip_block_comments(text: str) -> str:
            # Replace each `<# ... #>` block with the same number of newlines so
            # subsequent line numbering matches the original file.
            def _blank_keep_lines(match: "re.Match[str]") -> str:
                return "\n" * match.group(0).count("\n")
            return re.sub(r"<#.*?#>", _blank_keep_lines, text, flags=re.DOTALL)

        scannable = _strip_block_comments(self.body)
        raw_calls: list[tuple[int, str, str]] = []
        for idx, line in enumerate(scannable.splitlines(), start=1):
            for match in re.finditer(r"&\s+\$([A-Za-z_][A-Za-z0-9_]*)", line):
                var = match.group(1)
                if var in allowlist:
                    continue
                if "Invoke-NativeCommand" in line:
                    continue
                raw_calls.append((idx, var, line.strip()))
        self.assertEqual(
            [],
            raw_calls,
            "Every `& $native_exe` invocation must route through Invoke-NativeCommand. "
            "Raw calls found: "
            + "; ".join(f"line {ln} (${v}): {snippet!r}" for ln, v, snippet in raw_calls),
        )


if __name__ == "__main__":
    unittest.main()
