# winget publishing — EvilHumphrey.LegendCTL

LegendCTL is published in the
[Windows Package Manager community repository](https://github.com/microsoft/winget-pkgs/tree/master/manifests/e/EvilHumphrey/LegendCTL).
That upstream directory is the source of truth for versions users receive with:

```powershell
winget install --id EvilHumphrey.LegendCTL --exact
winget show --id EvilHumphrey.LegendCTL --exact
```

The versioned folders beside this file are historical submission snapshots.
They are not the live catalog and must not be copied forward as a new release
template. Microsoft accepted the package at 2.6.0; future updates are generated
from the accepted upstream manifest.

## Automated update workflow

`.github/workflows/winget-publish.yml` submits a version update only after a
stable GitHub release is published. This keeps the release-build safety boundary
intact: `release-build.yml` still creates a draft, the maintainer still
hardware-smokes the exact CI-built installer, and winget automation sees the
release only after the maintainer publishes it.

Configure the secret first, then enable the workflow with the variable:

1. An Actions secret named `WINGET_CREATE_GITHUB_TOKEN`. Microsoft WingetCreate
   currently requires a classic GitHub PAT with the `public_repo` scope for
   automated submissions; fine-grained tokens are not supported.
2. An Actions variable named `WINGET_AUTOMATION_ENABLED` with the exact value
   `true`.

The job is skipped while the variable is unset or set to `false`; if it is
enabled without the secret, the final submission step fails closed. Once both
settings are ready, rehearse from Actions with `workflow_dispatch`, leaving
**`dry_run` checked** — that generates and fully verifies the manifest but stops
before opening any pull request. Unchecking `dry_run` submits for real.

## Safety gates

Before a submission, the workflow:

- runs only in this repository, and only while `WINGET_AUTOMATION_ENABLED` is
  exactly `true`;
- accepts only `vMAJOR.MINOR.PATCH` tags;
- **refuses any tag that is not the latest published release**, so a submission
  can only move the catalog forward and can never rewrite a version Microsoft
  has already merged;
- re-reads GitHub's release object and rejects drafts and prereleases;
- requires exactly one expected Setup asset, matched case-sensitively, carrying
  a GitHub SHA-256 digest;
- **downloads that installer and binds it to this project's own build
  provenance** before generating anything — the bytes must hash to the digest
  GitHub recorded, must match the release's published `SHA256SUMS.txt`, and must
  pass `gh attestation verify` against this repository. Without this the
  submitted `InstallerSha256` would only be self-consistent with whatever the
  URL happened to serve;
- installs the exact .NET 9.0.316 SDK through a full-SHA-pinned official
  `actions/setup-dotnet` step;
- downloads Microsoft's WingetCreate v1.12.13.0 from its immutable release URL
  and verifies SHA-256
  `24042bd37915805615e6cf969ac57c6439124c3fe85823327f5f3fb24bd9ffea`, then
  re-verifies it immediately before the token-bearing submit step;
- generates from the accepted upstream package, pins `ReleaseDate`,
  `LicenseUrl`, and `ReleaseNotesUrl` to the published release, and refuses a
  manifest carrying a `DisplayVersion` other than the one being published;
- passes the PAT only through WingetCreate's recommended environment variable,
  and only to the final submit step.

A note on the token: WingetCreate needs a **classic** PAT, and `public_repo`
grants write to every public repository the account owns — including this one.
Issuing it from a dedicated account that owns nothing but a `winget-pkgs` fork,
with an expiry set, keeps that blast radius off the main account.

The resulting pull request still goes through microsoft/winget-pkgs validation
and moderator review. If automation is disabled, prepare the same three-file
manifest set manually, run
`winget validate --manifest <manifest-directory>`, and open a one-version-only
pull request against `microsoft/winget-pkgs:master`.
