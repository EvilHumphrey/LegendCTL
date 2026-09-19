[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $ManifestDirectory,
  [Parameter(Mandatory)] [string] $InstallerUrl,
  [Parameter(Mandatory)] [string] $InstallerSha256
)

$ErrorActionPreference = 'Stop'

if ($InstallerSha256 -cnotmatch '^[0-9a-fA-F]{64}\z') {
  throw 'The verified installer SHA-256 is missing or malformed.'
}
if ($InstallerUrl -cnotmatch '^https://github\.com/EvilHumphrey/LegendCTL/releases/download/v(?<version>[0-9]+\.[0-9]+\.[0-9]+)/ZDUltimateLegend-v\k<version>-Setup\.exe\z') {
  throw 'The verified installer URL is not an expected release asset URL.'
}
$version = $Matches.version

$directory = (Get-Item -LiteralPath $ManifestDirectory).FullName
$files = @(Get-ChildItem -LiteralPath $directory -Recurse -File)
$expectedNames = @(
  'EvilHumphrey.LegendCTL.yaml',
  'EvilHumphrey.LegendCTL.installer.yaml',
  'EvilHumphrey.LegendCTL.locale.en-US.yaml'
)
if ($files.Count -ne 3) { throw 'Expected exactly three manifest files.' }
foreach ($file in $files) {
  if ($file.Directory.FullName -cne $directory -or $file.Name -cnotin $expectedNames) {
    throw 'Unexpected manifest file or nested manifest directory.'
  }
  $rootFields = @()
  foreach ($line in Get-Content -LiteralPath $file.FullName) {
    if ($line -match '[\t\x85\u2028\u2029]') { throw 'Unsupported YAML whitespace.' }
    if ($line -match '^ *(?:#.*)?\z' -or $line.StartsWith(' ') -or $line.StartsWith('- ')) { continue }
    if ($line -cnotmatch '^(?<key>[A-Za-z][A-Za-z0-9]*):(?: (?<value>.*))?\z') {
      throw 'Unsupported YAML root key or directive.'
    }
    $rootFields += [pscustomobject]@{ Key = $Matches.key; Value = $Matches.value }
  }
  $manifestType = switch -CaseSensitive ($file.Name) {
    'EvilHumphrey.LegendCTL.yaml' { 'version' }
    'EvilHumphrey.LegendCTL.installer.yaml' { 'installer' }
    'EvilHumphrey.LegendCTL.locale.en-US.yaml' { 'defaultLocale' }
  }
  $requiredFields = @{
    PackageIdentifier = 'EvilHumphrey.LegendCTL'
    PackageVersion = $version
    ManifestType = $manifestType
  }
  foreach ($key in $requiredFields.Keys) {
    $found = @($rootFields | Where-Object { $_.Key -ieq $key })
    if ($found.Count -ne 1 -or $found[0].Key -cne $key -or $found[0].Value -cne $requiredFields[$key]) {
      throw "Unexpected or duplicate $key in $($file.Name)."
    }
  }
}

$path = Join-Path $directory 'EvilHumphrey.LegendCTL.installer.yaml'
$lines = @(Get-Content -LiteralPath $path)
$fields = @()
for ($index = 0; $index -lt $lines.Count; $index++) {
  $line = $lines[$index]
  if ($line -match '^ *(?:#.*)?\z') { continue }
  # This is a guard for the pinned generator's block-style YAML, not a YAML
  # parser. Fail closed on complex keys, flow maps, anchors, aliases, tags,
  # directives and multiline scalars; none are needed by this installer.
  if ($line -cnotmatch '^(?<indent> *)(?<item>- )?(?<key>[A-Za-z][A-Za-z0-9]*):(?: (?<value>.*))?\z') {
    if ($line -cmatch '^ *- [A-Za-z][A-Za-z0-9.]*\z') { continue }
    throw "Unsupported generated YAML syntax at line $($index + 1)."
  }
  $field = [pscustomobject]@{
    Key = $Matches.key
    Value = $Matches.value
    Indent = $Matches.indent.Length
    Item = $Matches.item -eq '- '
    Line = $index
  }
  $value = $field.Value
  # Accept complete one-line quoted strings in unrelated metadata (e.g.
  # ProductCode), but never interpret an unfinished quote as plain YAML.
  if ($value -and $value -cnotmatch '^(?:''(?:[^'']|'''')*''|"(?:[^"\\]|\\.)*"|[^\s''"\[\]{}&*!|>@`%?#][^\r\n]*)\z') {
    throw "Unsupported generated YAML scalar at line $($index + 1)."
  }
  $fields += $field
}

$installers = @($fields | Where-Object { $_.Key -ceq 'Installers' })
if ($installers.Count -ne 1 -or $installers[0].Indent -ne 0 -or
    $installers[0].Item -or $installers[0].Value) {
  throw 'Expected one root Installers block.'
}
$start = $installers[0].Line
$end = $lines.Count
foreach ($field in $fields) {
  if ($field.Line -gt $start -and $field.Indent -eq 0 -and -not $field.Item) {
    $end = $field.Line
    break
  }
}
$architectures = @($fields | Where-Object { $_.Key -ceq 'Architecture' })
$urls = @($fields | Where-Object { $_.Key -ceq 'InstallerUrl' })
$hashes = @($fields | Where-Object { $_.Key -ceq 'InstallerSha256' })
if ($architectures.Count -ne 1 -or $urls.Count -ne 1 -or $hashes.Count -ne 1) {
  throw 'Expected one architecture, installer URL and installer SHA-256.'
}
$architecture = $architectures[0]
if (-not $architecture.Item -or $architecture.Indent -notin @(0, 2) -or
    $architecture.Line -le $start -or $architecture.Line -ge $end -or
    $architecture.Value -cne 'x64') {
  throw 'Expected one x64 installer in the generated Installers block.'
}
$indent = $architecture.Indent + 2
for ($index = $start + 1; $index -lt $end; $index++) {
  $line = $lines[$index]
  if ($line -match '^ *(?:#.*)?\z' -or $index -eq $architecture.Line) { continue }
  if ($line -cnotmatch ('^ {' + $indent + ',}[^ ]')) {
    throw 'Unexpected additional installer or root field in Installers.'
  }
}
foreach ($field in @($urls[0], $hashes[0])) {
  if ($field.Item -or $field.Indent -ne $indent -or
      $field.Line -le $architecture.Line -or $field.Line -ge $end) {
    throw 'Installer URL and hash must belong to the single x64 installer.'
  }
}
if ($urls[0].Value -cne $InstallerUrl) {
  throw 'Generated InstallerUrl differs from the verified download.'
}
if ($hashes[0].Value -cnotmatch '^[0-9a-fA-F]{64}\z' -or
    $hashes[0].Value -ine $InstallerSha256) {
  throw 'Generated InstallerSha256 differs from the verified download.'
}
Write-Host 'Generated x64 installer URL and SHA-256 match the verified download.'
