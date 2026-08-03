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
settings are ready, the workflow can be rehearsed from Actions with
`workflow_dispatch` and an already-published stable tag.

## Safety gates

Before a submission, the workflow:

- accepts only `vMAJOR.MINOR.PATCH` tags;
- re-reads GitHub's release object and rejects drafts and prereleases;
- requires exactly one expected Setup asset with a GitHub SHA-256 digest;
- installs the exact .NET 9.0.316 SDK through a full-SHA-pinned official
  `actions/setup-dotnet` step;
- downloads Microsoft's WingetCreate v1.12.13.0 from its immutable release URL
  and verifies SHA-256
  `24042bd37915805615e6cf969ac57c6439124c3fe85823327f5f3fb24bd9ffea`;
- generates from the accepted upstream package and then pins `ReleaseDate`,
  `LicenseUrl`, and `ReleaseNotesUrl` to the published release;
- passes the PAT only through WingetCreate's recommended environment variable,
  and only to the final submit step.

The resulting pull request still goes through microsoft/winget-pkgs validation
and moderator review. If automation is disabled, prepare the same three-file
manifest set manually, run
`winget validate --manifest <manifest-directory>`, and open a one-version-only
pull request against `microsoft/winget-pkgs:master`.
