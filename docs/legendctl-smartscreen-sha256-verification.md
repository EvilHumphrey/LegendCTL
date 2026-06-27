---
title: "LegendCTL SmartScreen & SHA-256 Verification"
description: "Why unsigned LegendCTL builds may trigger SmartScreen, and how to verify the Windows download with SHA-256 before running it."
---

# LegendCTL on Windows: SmartScreen, unsigned builds & verifying the SHA-256

If Windows showed you a **"Windows protected your PC"** prompt, or you just want to check a download before running it,
this page is the concrete path. [LegendCTL](https://github.com/EvilHumphrey/LegendCTL) is a free, open-source, fully
local Windows app for the ZD Ultimate Legend — and the smart move with *any* unsigned utility is to verify rather than
trust.

**Quick answer:** SmartScreen appears because the build is currently **unsigned**, not because it's known to be
harmful. Download only from the official release page, **verify the SHA-256** of the file against the published
`SHA256SUMS.txt`, then choose **More info → Run anyway**.

## Why SmartScreen can appear on unsigned builds

Microsoft Defender SmartScreen warns on programs that don't yet have download **reputation**. New, unsigned apps from
new publishers haven't built that reputation, so the prompt is expected — it is not, by itself, a malware verdict.
Reputation accrues over time; code signing (planned, below) starts that clock but doesn't remove the prompt instantly.

## Download only from the project's release source

Get builds **only** from the official releases page:
<https://github.com/EvilHumphrey/LegendCTL/releases> (the latest is marked **Latest**). Avoid third-party mirrors and
"download sites" — if you didn't get it from the link above, don't trust it. Each release publishes two artifacts plus
a checksum file:

- `ZDUltimateLegend-v<version>-windows.zip` — portable ZIP (simplest; no admin)
- `ZDUltimateLegend-v<version>-Setup.exe` — installer
- `SHA256SUMS.txt` — the official SHA-256 of both files

## What SHA-256 verification proves — and what it doesn't

- **It proves integrity:** the file you downloaded is byte-for-byte the one the project published (not corrupted, not
  tampered with in transit, not a swapped-out copy).
- **It does not, by itself, prove the file is safe.** A hash only ties your file to the published one. For *safety*,
  the open-source angle is what helps: read the source, build it yourself, and run the no-network check (below).

## Verify the ZIP or installer on Windows

Open PowerShell in your Downloads folder and run (substitute your file name):

```powershell
Get-FileHash .\ZDUltimateLegend-v2.0.2-windows.zip -Algorithm SHA256
```

That prints a 64-character hash. Open `SHA256SUMS.txt` from the same release and find the line for your file.

## Match the hash before running the file

Open `SHA256SUMS.txt` from the same release, find the line for your file, and compare it to the hash you got
(case-insensitive). To check it programmatically, paste the published hash for **your** download:

```powershell
# Paste the SHA-256 for your file from the release's SHA256SUMS.txt:
$published = '<paste-from-SHA256SUMS.txt>'
(Get-FileHash .\ZDUltimateLegend-v2.0.2-windows.zip -Algorithm SHA256).Hash -eq $published   # True = match
```

Always use the hash from **the same release you downloaded** — hashes change with every release, so don't reuse one
from an older version or another page. If it matches, the file is intact. If it **doesn't** match, stop — don't run
it; re-download from the official page and, if it still mismatches,
[report it](#where-to-report-a-hash-mismatch-or-suspicious-download).

## My antivirus flagged it — is it a virus?

Most likely a **false positive** — but verify rather than assume. LegendCTL is packaged with PyInstaller, whose
self-extracting bundle pattern is a well-known source of heuristic AV false positives; confirm the SHA-256 (above) and,
if anything still looks off, don't run it. You can cross-check the file on
[VirusTotal](https://www.virustotal.com/) against many engines (note: VirusTotal submissions are shared with the
security community). If you'd rather run **zero** packaged binaries, [build from source](../README.md#build-from-source).

## Open-source checks: source, release, and local-only docs

Beyond the hash, you can verify the project's claims directly:

- **Read the source** — it's all on [GitHub](https://github.com/EvilHumphrey/LegendCTL), MIT licensed.
- **Build it yourself** — see [Build from source](../README.md#build-from-source).
- **The "deliberately absent" constraints** (no network, no input injection, …) are enforced by the test suite, not
  just promised.

## What LegendCTL does not install

No kernel **driver**, no **virtual gamepad**, no **background service** or scheduled task. It runs only while its
window is open. The **portable ZIP** adds no Start-Menu or uninstaller entry — delete the extracted folder to remove
the app binaries. Note that your local settings, profiles, and logs persist under `%APPDATA%\ZDUltimateLegend\`
regardless of where the folder lives, so remove that separately if you want them gone too.

## After launch: verify no network activity

The LegendCTL process makes **zero** network calls. You don't have to take that on faith — confirm it in a couple of
minutes with the built-in check in [verifying-no-network.md](verifying-no-network.md) (the only outbound action
anywhere is the About screen's GitHub links, which hand a URL to your default browser; the app opens no socket itself).

## Code signing is planned

Signing via the free [SignPath Foundation](https://signpath.org) program for open-source projects is planned once the
project is eligible — see the [code-signing policy](code-signing-policy.md). Until then, builds ship unsigned with
published hashes so you can verify integrity yourself.

## Where to report a hash mismatch or suspicious download

If a download's hash doesn't match (after re-downloading from the official page), please report it — open a
[security advisory](https://github.com/EvilHumphrey/LegendCTL/security/advisories/new) (private) for anything that
looks like tampering, or a normal issue for a plain mismatch. Include where you downloaded it and the hash you got.

## See also

- [Official ZD app vs LegendCTL: which tool to use](official-zd-app-vs-legendctl.md)
- [Verifying no network access](verifying-no-network.md)
- [FAQ](FAQ.md) — "is it safe?", "why unsigned?", "what does it install?"
- [README](../README.md) — download and release notes
