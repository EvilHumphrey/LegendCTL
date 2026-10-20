<#
install_local.ps1 — mirror the latest dist build to a stable local-install
folder and (re)create the Desktop + Start Menu shortcuts pointing at it.

The release builder versions its output (dist\ZDUltimateLegend-v<version>\),
so a shortcut pointed straight into dist\ would need updating for a new version.
This explicit helper mirrors the
freshest build to a fixed path (<repo>\local-install\) that the shortcuts can
rely on, then refreshes the shortcuts so they always launch the current build.

Run it separately after reviewing a successful build in the standard dist
layout. The release builder does not call this helper or update shortcuts.
#>
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "release_paths.ps1")
$distRoot = Join-Path $repoRoot "dist"
$installDir = Join-Path $repoRoot "local-install"
Assert-ReleasePath -LiteralPath $distRoot -Within $repoRoot -Recurse
Assert-ReleasePath -LiteralPath $installDir -Within $repoRoot -Recurse -RejectHardLinks

# 1. Find the freshest dist build folder.
$latestBuild = Get-ChildItem -LiteralPath $distRoot -Directory -Filter "ZDUltimateLegend-v*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $latestBuild) {
    Write-Error "No build found under dist\ZDUltimateLegend-v*. Run tools\build_release.ps1 first."
}

$exeName = "ZD Ultimate Legend.exe"
if (-not (Test-Path (Join-Path $latestBuild.FullName $exeName))) {
    Write-Error "Build folder '$($latestBuild.Name)' has no '$exeName'."
}

# 2. Mirror it to a stable location while removing stale files from prior builds.
Assert-ReleasePath -LiteralPath $latestBuild.FullName -Within $distRoot -Recurse
Assert-ReleasePath -LiteralPath $installDir -Within $repoRoot -Recurse -RejectHardLinks
if (-not ('LegendCtlReleaseMirror' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot "release_mirror.cs")
}
$mirror = New-Object LegendCtlReleaseMirror($latestBuild.FullName, $installDir)
try {
    $mirror.Prepare()
    $mirror.Publish()
} finally {
    $mirror.Dispose()
}

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
