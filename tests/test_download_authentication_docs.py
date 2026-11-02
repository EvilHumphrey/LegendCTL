"""Regression guards for public download-authentication guidance.

An unsigned artifact and its checksum arrive through the same release channel.
The checksum can detect a mismatch relative to the supplied value, but cannot
authenticate the publisher if that channel is compromised.  Current attested
releases use GitHub build provenance as the separate origin check.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUIDANCE = (
    "README.md",
    ".github/SECURITY.md",
    "docs/install-windows-and-smartscreen.md",
    "docs/legendctl-smartscreen-sha256-verification.md",
    "docs/code-signing-policy.md",
    "docs/FAQ.md",
    "docs/REVIEWER-KIT.md",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8").casefold()


class DownloadAuthenticationDocsTests(unittest.TestCase):
    def test_public_guidance_distinguishes_checksum_from_authentication(self) -> None:
        combined = re.sub(
            r"\s+", " ", "\n".join(_read(relative) for relative in GUIDANCE)
        )
        self.assertIn("same release channel", combined)
        self.assertIn("does not authenticate the publisher", combined)
        self.assertIn("not publisher authentication", combined)
        self.assertIn("not the equivalent of an authenticode signature", combined)

        for relative in (
            "README.md",
            ".github/SECURITY.md",
            "docs/install-windows-and-smartscreen.md",
            "docs/legendctl-smartscreen-sha256-verification.md",
            "docs/FAQ.md",
            "docs/REVIEWER-KIT.md",
        ):
            with self.subTest(relative=relative):
                body = _read(relative).replace("-", " ")
                self.assertIn("publisher", body)
                self.assertTrue(
                    "authenticate" in body or "authentication" in body,
                    f"{relative} must state the publisher-authentication limit",
                )

    def test_install_guides_remove_checksum_authentication_overclaims(self) -> None:
        forbidden_claims = (
            "verify your download is genuine",
            "verifying your download is genuine",
            "same integrity guarantee a signature provides",
            "the real, untampered file",
            "if they match, the file is exactly what was published",
            "it proves integrity",
            "tampered with in transit",
            "swapped-out copy",
            "file is intact",
            "published hashes so you can verify integrity",
        )
        for relative in (
            "docs/install-windows-and-smartscreen.md",
            "docs/legendctl-smartscreen-sha256-verification.md",
        ):
            with self.subTest(relative=relative):
                body = re.sub(r"\s+", " ", _read(relative))
                for forbidden in forbidden_claims:
                    self.assertNotIn(forbidden, body)
                self.assertIn("not a reason to bypass smartscreen", body)

    def test_hash_is_not_presented_as_run_anyway_authorization(self) -> None:
        for relative in GUIDANCE:
            with self.subTest(relative=relative):
                body = _read(relative).replace("\n", " ")
                self.assertIsNone(
                    re.search(
                        r"(?:verify|check) the (?:published )?sha-?256.{0,120}"
                        r"(?:then|and then) (?:choose |click )?\*\*more info",
                        body,
                    ),
                    f"{relative} still makes a checksum the SmartScreen authorization gate",
                )

    def test_attestation_origin_path_remains_documented(self) -> None:
        for relative in (
            ".github/SECURITY.md",
            "docs/install-windows-and-smartscreen.md",
            "docs/legendctl-smartscreen-sha256-verification.md",
            "docs/code-signing-policy.md",
            "docs/FAQ.md",
            "docs/REVIEWER-KIT.md",
        ):
            with self.subTest(relative=relative):
                self.assertIn("attestation", _read(relative))
        install = _read("docs/install-windows-and-smartscreen.md")
        self.assertIn("gh attestation verify", install)
        self.assertIn("--signer-workflow", install)

    def test_every_public_attestation_command_pins_workflow_and_release_tag(self) -> None:
        commands: list[tuple[str, str]] = []
        for path in ROOT.rglob("*.md"):
            relative = path.relative_to(ROOT).as_posix()
            body = path.read_text(encoding="utf-8").casefold()
            for command in re.findall(
                r"(?ms)^gh attestation verify .+?(?=^gh attestation verify |^```)",
                body,
            ):
                commands.append((relative, command))

        self.assertEqual(len(commands), 4)
        for relative, command in commands:
            with self.subTest(relative=relative, command=command.splitlines()[0]):
                self.assertIn("--repo evilhumphrey/legendctl", command)
                self.assertIn(
                    "--signer-workflow evilhumphrey/legendctl/.github/workflows/release-build.yml",
                    command,
                )
                self.assertIn("--source-ref refs/tags/v<version>", command)

    def test_attestation_prerequisites_cover_policy_flags_and_ps51_encoding(self) -> None:
        install = _read("docs/install-windows-and-smartscreen.md")
        self.assertIn("v2.102.0 or newer", re.sub(r"\s+", " ", install))
        self.assertNotIn("gh attestation trusted-root >", install)
        self.assertIn("$lastexitcode -ne 0", install)
        self.assertIn("new-object system.text.utf8encoding($false)", install)
        self.assertIn("[io.file]::writealltext", install)
        self.assertIn(
            "cannot detect whether either name was later renamed or reused",
            re.sub(r"\s+", " ", install),
        )


if __name__ == "__main__":
    unittest.main()
