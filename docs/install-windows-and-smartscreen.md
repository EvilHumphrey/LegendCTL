---
title: Installing LegendCTL on Windows (and the SmartScreen warning)
description: How to download, verify, and run LegendCTL on Windows — including why Windows shows a SmartScreen warning for unsigned apps and how to verify your download is genuine.
---

# Installing LegendCTL on Windows

LegendCTL is a free, open-source, standalone configurator for the ZD Ultimate Legend — no official ZD app required, no drivers, no background service, no telemetry. This page covers downloading it, getting past the Windows SmartScreen warning, and **verifying your download is genuine** so you don't have to take "unsigned" on faith.

## 1. Download

From the [Releases page](https://github.com/EvilHumphrey/LegendCTL/releases/latest), grab one of:

- **`ZDUltimateLegend-v<version>-windows.zip`** — portable. Unzip anywhere and run `ZD Ultimate Legend.exe`. Nothing is installed; delete the folder to remove it. **Recommended if you just want to try it.**
- **`ZDUltimateLegend-v<version>-Setup.exe`** — installer (adds Start-Menu / Desktop shortcuts; uninstall via Settings → Apps).

## 2. Why Windows shows "Windows protected your PC"

LegendCTL is **not code-signed** (a code-signing certificate is gated, for an open-source project, on first building download reputation — see below). So Windows SmartScreen shows a blue **"Windows protected your PC"** dialog for it, the same as it does for most new, independent software.

**This warning is about reputation, not safety** — it means "Windows hasn't seen this file downloaded by enough people yet," not "this file is harmful." You don't have to trust that blindly, though — you can **verify the download yourself** (next section).

To run it:

1. Click **More info** in the SmartScreen dialog.
2. Click **Run anyway**.

*(On a managed/work PC, an administrator policy can block "Run anyway" entirely — that's expected on locked-down machines and isn't specific to LegendCTL.)*

## 3. Verify your download is genuine (recommended)

Because LegendCTL is unsigned, the honest way to be sure your download is the real, untampered file is to check its SHA-256 hash against the one published with the release.

1. Each release includes a **`SHA256SUMS.txt`** asset listing the official hashes.
2. Open PowerShell in your downloads folder and run:

   ```powershell
   Get-FileHash .\ZDUltimateLegend-v<version>-Setup.exe -Algorithm SHA256
   ```

3. Compare the printed hash to the matching line in `SHA256SUMS.txt`. **If they match, the file is exactly what was published.** If they don't match, delete it and re-download.

This is the same integrity guarantee a signature provides, done in the open — fitting for a tool whose whole point is being honest about what it is.

## 4. Why you can trust it beyond the hash

- **Open source.** The full source is on [GitHub](https://github.com/EvilHumphrey/LegendCTL) — read exactly what it does.
- **No telemetry, no network calls.** It talks only to your controller over USB/HID; it doesn't phone home.
- **No drivers, no background service.** It's a plain desktop app; closing it stops it completely.
- **Standalone.** It doesn't need (or touch) the official ZD app.

## 5. Install via winget (when available)

Once the manifest is accepted into the Windows Package Manager community repo, you'll be able to install and update with:

```powershell
winget install EvilHumphrey.LegendCTL
```

winget installs are a smoother path because the package is fetched and hash-checked for you.

## 6. Uninstall

- **Portable ZIP:** delete the folder.
- **Installer:** Settings → Apps → "ZD Ultimate Legend Wrapper" → Uninstall. (User data lives in `%APPDATA%\ZDUltimateLegend\`; delete that folder too if you want a completely clean removal.)
