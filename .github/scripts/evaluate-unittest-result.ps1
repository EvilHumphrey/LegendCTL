[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $LogPath,

  [Parameter(Mandatory = $true)]
  [int64] $ProcessExitCode
)

$ErrorActionPreference = 'Stop'

try {
  $log = Get-Content -LiteralPath $LogPath -Raw
}
catch {
  Write-Host "::error::Could not read unittest log at ${LogPath}: $($_.Exception.Message)"
  exit 1
}

if ($log -match '(?m)^FAILED \(') {
  Write-Host "::error::unittest reported FAILED — see the summary above."
  exit 1
}

# A lone `OK` can be emitted by test code. Require unittest's paired terminal
# summary shape instead: `Ran <N> tests ...`, a blank line, then `OK (...)`.
$okSummary = [regex]::Matches(
  $log,
  '(?s)(?:^|\r?\n)Ran\s+\d+\s+tests?\s+in\s+[^\r\n]+\r?\n\r?\nOK(?: \([^\r\n]*\))?(?:\r?\n)?\z'
)
if ($okSummary.Count -eq 0) {
  Write-Host "::error::No complete unittest 'Ran ...' + 'OK' summary found (exit code $ProcessExitCode)."
  exit 1
}

if ($ProcessExitCode -eq 0) {
  Write-Host "Suite PASSED. (process exit code = 0)"
  exit 0
}
if ($ProcessExitCode -in @(139, -1073741819, 3221225477)) {
  Write-Host "::warning::Suite passed, then hit the known Dear PyGui teardown artifact (exit code $ProcessExitCode)."
  exit 0
}

Write-Host "::error::unittest reported OK but exited with unexpected code $ProcessExitCode; refusing to hide a post-summary failure."
exit 1
