# winget manifest — EvilHumphrey.LegendCTL

This directory holds the [winget](https://learn.microsoft.com/windows/package-manager/)
package manifest for the **public LegendCTL release**, so that
`winget install EvilHumphrey.LegendCTL` (or `winget install --id EvilHumphrey.LegendCTL`)
works once it is merged into the community repo. No code-signing certificate is
required — winget only needs a public installer URL and its SHA256.

The version subfolder (`2.0.3/`) contains the three manifest files that get
copied verbatim into a `microsoft/winget-pkgs` fork. **Copy only the `.yaml`
files** — this `README.md` stays here.

```
2.0.3/
  EvilHumphrey.LegendCTL.installer.yaml     # installer manifest (url, sha256, inno, ProductCode)
  EvilHumphrey.LegendCTL.locale.en-US.yaml  # default-locale manifest (publisher, description, tags)
  EvilHumphrey.LegendCTL.yaml               # version manifest (ties the set together)
```

## Provenance (every asserted value, and where it came from)

| Field | Value | Source |
|---|---|---|
| InstallerUrl | `https://github.com/EvilHumphrey/LegendCTL/releases/download/v2.0.3/ZDUltimateLegend-v2.0.3-Setup.exe` | `gh release view v2.0.3 -R EvilHumphrey/LegendCTL` asset `url` |
| InstallerSha256 | `1525422B7F451C4516E4DDE137860398EAA160E30A16C7E36320B6151E55A735` | Release `SHA256SUMS.txt`, cross-checked against GitHub's API asset `digest` (`sha256:1525422b…`) — both agree |
| InstallerType | `inno` | `tools/installer/inno_setup_zd_wrapper.iss` (Inno Setup script) |
| ProductCode | `{ZDUltimateLegend}_is1` | Inno `AppId={{ZDUltimateLegend}` → resolved AppId `{ZDUltimateLegend}` → Inno appends `_is1` for the ARP/uninstall key |
| AppsAndFeaturesEntries DisplayName | `ZD Ultimate Legend Wrapper` | Inno `AppName` (`.iss` line 21) — the legacy EXE name the installer still registers |
| AppsAndFeaturesEntries Publisher | `EvilHumphrey` | Inno `AppPublisher` (`.iss` line 23) |
| AppsAndFeaturesEntries DisplayVersion | `2.0.3` | Inno `AppVersion` = `ZDUL_VERSION` build env = release version |
| Scope | `machine` | Inno `PrivilegesRequired=admin` + `DefaultDirName={autopf}\ZDUltimateLegend` (Program Files) |
| Architecture | `x64` | Inno `ArchitecturesAllowed=x64compatible` |

### On the ProductCode / AppId (the one place precision matters)

In an Inno `.iss`, a leading `{{` is the escape for a literal `{`. So
`AppId={{ZDUltimateLegend}` resolves to the AppId **`{ZDUltimateLegend}`** (braces
included), and Inno registers its uninstall key as `{AppId}_is1` →
**`{ZDUltimateLegend}_is1`**. This braces-are-kept behaviour was confirmed against
real Inno apps already installed on the build machine (e.g. ExitLag's
`{58571ef5-…}_is1`, Revo's `{A28DBDA2-…}_is1` — braced AppIds keep their braces in
the key; unbraced ones like `Git_is1` do not). winget uses this value to detect
installed/upgradeable/uninstallable state, so it must match exactly.

> The `{ZDUltimateLegend}_is1` key was **not** present in this machine's registry
> at authoring time (the operator has only used the portable/local-install path,
> never the actual Setup.exe), so the value is derived from the `.iss` rather than
> read live. It is stable across versions because the AppId is a hardcoded literal
> in the only `.iss`, not version-parameterized.

### One chosen-convention field

`MinimumOSVersion: 10.0.17763.0` is a pragmatic "modern Windows 10 (1809+)" floor.
The project does **not** document an exact minimum build, and the app is x64-only
(DearPyGui / Python 3.12). Lower it to `10.0.0.0` for maximum reach, or raise it,
if you prefer — it only gates which Windows builds winget will install on.

## Validation

```
> winget validate --manifest <this>/2.0.3
Manifest validation succeeded.
```
(Validated locally with winget v1.28.240.)

## How to submit (operator does this — not the agent)

1. **Fork** `https://github.com/microsoft/winget-pkgs` on GitHub (or reuse an
   existing fork). Clone it locally and add the upstream remote if you want to
   keep it current:
   ```powershell
   git clone https://github.com/<you>/winget-pkgs.git
   cd winget-pkgs
   git checkout -b legendctl-2.0.3
   ```
2. **Place the files** at the path winget-pkgs expects (publisher's first letter,
   lowercase, then PackageIdentifier parts, then version):
   ```
   manifests/e/EvilHumphrey/LegendCTL/2.0.3/EvilHumphrey.LegendCTL.installer.yaml
   manifests/e/EvilHumphrey/LegendCTL/2.0.3/EvilHumphrey.LegendCTL.locale.en-US.yaml
   manifests/e/EvilHumphrey/LegendCTL/2.0.3/EvilHumphrey.LegendCTL.yaml
   ```
   (Copy the three `.yaml` files from this repo's `packaging/winget/2.0.3/`.)
3. **Re-validate** in place, and optionally test-install in Windows Sandbox:
   ```powershell
   winget validate --manifest manifests/e/EvilHumphrey/LegendCTL/2.0.3
   # optional, requires Windows Sandbox enabled — installs the package for real:
   winget install --manifest manifests/e/EvilHumphrey/LegendCTL/2.0.3
   ```
4. **Commit and push to your fork**, then open a PR against
   `microsoft/winget-pkgs:master`:
   ```powershell
   git add manifests/e/EvilHumphrey/LegendCTL/2.0.3
   git commit -m "New package: EvilHumphrey.LegendCTL version 2.0.3"
   git push -u origin legendctl-2.0.3
   ```
   Open the PR on GitHub. The winget-pkgs CI runs schema validation **and** an
   automated Windows Sandbox install/uninstall smoke test; a moderator merges
   once they pass.

**Shortcut alternative:** `wingetcreate submit <dir>` (from Microsoft's
`wingetcreate` tool) forks, branches, and opens the PR in one step. Either path
ends in the same community-repo PR — which only you (the operator) should open.

After merge, `winget install EvilHumphrey.LegendCTL` is the one-line install you
can drop into video descriptions and the README.
