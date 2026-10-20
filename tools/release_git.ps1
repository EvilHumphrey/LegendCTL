param([string]$ExpectedPath)

. (Join-Path $PSScriptRoot "release_paths.ps1")

function Resolve-TrustedReleaseGit {
    param([string]$ExpectedPath)

    # Do not autoload a same-named security module from inherited PSModulePath
    # (which can also contain incompatible PowerShell 7 modules in a PS 5 child).
    Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1") -ErrorAction Stop
    # The release boundary deliberately supports the protected Git for Windows
    # installation only. PATH, aliases, functions and per-user shims are ignored.
    $programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    # Authenticate the actual implementation, not cmd\git.exe's launcher.
    $candidate = Join-Path $programFiles "Git\mingw64\bin\git.exe"
    if ($ExpectedPath -and -not $candidate.Equals($ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release Git must be the approved Program Files Git executable."
    }
    Assert-ReleasePath -LiteralPath $candidate
    if (-not [IO.File]::Exists($candidate)) {
        throw "Release builds require Git for Windows in Program Files."
    }
    $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $candidate
    if (
        $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|, )O=Johannes Schindelin(,|$)'
    ) {
        throw "Release Git must have a valid approved Git for Windows Authenticode signature."
    }

    # Program Files is not a sufficient boundary if its ACL or this installation
    # has been weakened. Accept write/ownership authority only for administrators,
    # SYSTEM and Windows servicing; do not attempt to repair an ACL here.
    $trustedOwners = @(
        "S-1-5-18", "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )
    $writeRights = [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    $current = $candidate
    while ($current.Length -ge $programFiles.Length) {
        $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $current -ErrorAction Stop
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($trustedOwners -notcontains $owner) {
            throw "Release Git installation has an unapproved owner."
        }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and
                ($rule.FileSystemRights -band $writeRights) -ne 0 -and
                $trustedOwners -notcontains $rule.IdentityReference.Value
            ) {
                throw "Release Git installation grants unprivileged write access."
            }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    return $candidate
}


if ($MyInvocation.InvocationName -ne ".") {
    Resolve-TrustedReleaseGit -ExpectedPath $ExpectedPath
}
