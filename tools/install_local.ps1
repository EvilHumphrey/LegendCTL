<#
install_local.ps1 — mirror the latest dist build to a stable local-install
folder and (re)create the Desktop + Start Menu shortcuts pointing at it.

Why this exists: tools\build_release.ps1 wipes dist\ on every run and versions
its output (dist\ZDUltimateLegend-v<version>\), so a shortcut pointed straight
into dist\ would break on every rebuild or version bump. This mirrors the
freshest build to a fixed path (<repo>\local-install\) that the shortcuts can
rely on, then refreshes the shortcuts so they always launch the current build.

Run it standalone any time, or let build_release.ps1 call it automatically at
the end of a successful build (it does — see the tail of that script).
#>
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

# 1. Find the freshest dist build folder.
$latestBuild = Get-ChildItem -Path (Join-Path $repoRoot "dist") -Directory -Filter "ZDUltimateLegend-v*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $latestBuild) {
    Write-Error "No build found under dist\ZDUltimateLegend-v*. Run tools\build_release.ps1 first."
}

$exeName = "ZD Ultimate Legend.exe"
if (-not (Test-Path (Join-Path $latestBuild.FullName $exeName))) {
    Write-Error "Build folder '$($latestBuild.Name)' has no '$exeName'."
}

# 2. Mirror it to a stable location (survives dist\ cleaning + version bumps).
$installDir = Join-Path $repoRoot "local-install"
robocopy $latestBuild.FullName $installDir /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Error "robocopy mirror failed (exit $LASTEXITCODE). If the app is currently open, close it and re-run."
}
$global:LASTEXITCODE = 0  # robocopy exit codes 1-7 are success; reset so callers don't see a false failure

$installedExe = Join-Path $installDir $exeName
if (-not (Test-Path $installedExe)) {
    Write-Error "Mirror succeeded but '$installedExe' is missing."
}

# 3. (Re)create Desktop + Start Menu shortcuts pointing at the stable copy.
$shortcutName = "ZD Ultimate Legend.lnk"
$startMenuPrograms = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) $shortcutName),
    (Join-Path $startMenuPrograms $shortcutName)
)
foreach ($lnk in $targets) {
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath       = $installedExe
    $sc.WorkingDirectory = $installDir
    $sc.IconLocation     = "$installedExe,0"   # uses the EXE's embedded icon (default until a branded icon is set)
    $sc.Description       = "ZD Ultimate Legend Control Center"
    $sc.Save()
}

Write-Host "Installed '$($latestBuild.Name)' -> $installDir" -ForegroundColor Green
Write-Host "Shortcuts refreshed (Desktop + Start Menu) -> $installedExe" -ForegroundColor Green
