# Shared Inno Setup acquisition/use boundary. Dot-sourcing never runs a binary.
# The manifest is reviewed repository input, not metadata obtained at build time.

function Assert-InnoLocalPath {
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[A-Za-z]:\\') {
        throw 'Inno Setup paths must be on a local drive, not UNC/device paths.'
    }
    $part = $full
    while ($part) {
        $item = Get-Item -LiteralPath $part -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Inno Setup path contains a reparse point: $part"
        }
        $parent = [IO.Directory]::GetParent($part)
        $part = if ($parent) { $parent.FullName } else { $null }
    }
    return $full
}

function Read-InnoSetupPin {
    param([string]$Path = (Join-Path $PSScriptRoot 'inno_setup.lock.json'))
    $Path = Assert-InnoLocalPath -Path $Path
    $pin = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($pin.schema -ne 1 -or $pin.version -notmatch '^6\.[0-9]+\.[0-9]+$') {
        throw 'Invalid Inno Setup pin schema/version.'
    }
    $asset = "innosetup-$($pin.version).exe"
    $tag = 'is-' + $pin.version.Replace('.', '_')
    $expectedUrl = "https://github.com/jrsoftware/issrc/releases/download/$tag/$asset"
    if ($pin.installer.url -cne $expectedUrl -or $pin.installer.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $pin.installer.authenticodeSubject -cne 'CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL') {
        throw 'Invalid Inno Setup installer pin.'
    }
    # This is the root-level executable/compiler-input set reviewed for 6.7.1.
    # Omitting a required file from a malformed manifest must not weaken checks.
    $required = @(
        'Compil32.exe', 'ISCC.exe', 'ISCmplr.dll', 'ISPP.dll', 'ISSigTool.exe',
        'is7z.dll', 'is7zxa.dll', 'is7zxr.dll', 'isbunzip.dll', 'isbzip.dll',
        'islzma.dll', 'islzma32.exe', 'islzma64.exe', 'isscint.dll', 'isunzlib.dll', 'iszlib.dll',
        'Setup.e32', 'SetupCustomStyle.e32', 'SetupLdr.e32', 'SetupLdr.e64',
        'Default.isl', 'ISPPBuiltins.iss'
    )
    $signed = @(
        'ISCmplr.dll', 'ISPP.dll', 'is7z.dll', 'is7zxa.dll', 'is7zxr.dll', 'isbunzip.dll', 'isbzip.dll',
        'islzma.dll', 'islzma32.exe', 'islzma64.exe', 'isscint.dll', 'isunzlib.dll', 'iszlib.dll',
        'Setup.e32', 'SetupCustomStyle.e32', 'SetupLdr.e32', 'SetupLdr.e64'
    )
    $required += @($signed | ForEach-Object { "$_.issig" })
    $properties = @($pin.files.PSObject.Properties)
    if ($properties.Count -ne $required.Count) {
        throw 'Invalid Inno Setup compiler file set.'
    }
    foreach ($name in $required) {
        $property = $properties | Where-Object { $_.Name -ceq $name }
        if (-not $property -or $property.Value -isnot [string] -or $property.Value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Invalid Inno Setup compiler pin: $name"
        }
    }
    return $pin
}

function Assert-InnoSetupCompiler {
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)]$Pin
    )
    $root = Assert-InnoLocalPath -Path $Directory
    if (-not (Get-Item -LiteralPath $root -Force -ErrorAction Stop).PSIsContainer) {
        throw 'Inno Setup compiler location is not a directory.'
    }
    foreach ($property in $Pin.files.PSObject.Properties) {
        $path = Assert-InnoLocalPath -Path (Join-Path $root $property.Name)
        if ((Get-Item -LiteralPath $path -Force -ErrorAction Stop).PSIsContainer) {
            throw "Inno Setup compiler input is not a file: $($property.Name)"
        }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($actual -ine $property.Value) {
            throw "Inno Setup compiler SHA-256 mismatch: $($property.Name)"
        }
    }
    # Do not permit an unreviewed DLL beside the verified executable to take
    # part in Windows DLL resolution. The normal uninstaller is not invoked.
    foreach ($item in Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop) {
        if (($item.Extension -ieq '.dll' -or $item.Extension -ieq '.exe' -or
             $item.Name -imatch '\.(local|manifest)$') -and
            $item.Name -notmatch '^unins[0-9]{3}\.exe$' -and
            $item.Name -cnotin @($Pin.files.PSObject.Properties.Name)) {
            throw "Unexpected executable input beside Inno Setup compiler: $($item.Name)"
        }
    }
    return (Join-Path $root 'ISCC.exe')
}

function Get-InnoSetupDirectory {
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)]$Pin)
    return (Join-Path $RepoRoot ".release-tools\inno-setup-$($Pin.version)")
}

function Find-VerifiedInnoSetupCompiler {
    param([Parameter(Mandatory)][string]$RepoRoot)
    $pin = Read-InnoSetupPin
    $candidates = @(
        (Get-InnoSetupDirectory -RepoRoot $RepoRoot -Pin $pin),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6')
    )
    foreach ($candidate in $candidates) {
        # A present but invalid directory is an error, never a reason to fall
        # back to another compiler. Do not run a banner or trust the registry.
        try { $null = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { continue }
        return (Assert-InnoSetupCompiler -Directory $candidate -Pin $pin)
    }
    return $null
}

function Assert-InnoSetupInstaller {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Pin)
    $Path = Assert-InnoLocalPath -Path $Path
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($hash -ine $Pin.installer.sha256) {
        throw 'Inno Setup installer SHA-256 mismatch; refusing to execute it.'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -cne $Pin.installer.authenticodeSubject) {
        throw 'Inno Setup installer publisher/signature mismatch; refusing to execute it.'
    }
}
