param(
    [string]$ReleaseDir = $null,
    [int]$DurationSeconds = 5
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ReleaseDir) {
    $latest = Get-ChildItem "$repoRoot\dist\ZDUltimateLegend-v*" -Directory |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        Write-Error "No release dir found in dist\"
    }
    $ReleaseDir = $latest.FullName
}

$exe = Join-Path $ReleaseDir "ZD Ultimate Legend.exe"
if (-not (Test-Path $exe)) {
    Write-Error "Exe not found at $exe"
}

$proc = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
Start-Sleep -Seconds $DurationSeconds
if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
    Write-Host "Smoke OK: process survived $DurationSeconds seconds." -ForegroundColor Green
} else {
    Write-Error "Smoke FAIL: process exited early with code $($proc.ExitCode)"
}
