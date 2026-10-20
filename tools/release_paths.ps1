# Shared checks for release output and local mirror paths. Do not resolve a
# reparse point first: that would erase the evidence that the path redirects.
function Assert-ReleasePath {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [string]$Within,
        [switch]$Recurse,
        [switch]$RejectHardLinks
    )

    if ($LiteralPath -notmatch '^[A-Za-z]:[\\/]' -or $LiteralPath.Substring(2).Contains(":")) {
        throw "Release paths must be ordinary absolute local drive paths."
    }
    $full = [IO.Path]::GetFullPath($LiteralPath).TrimEnd('\', '/')
    if ($Within) {
        $boundary = [IO.Path]::GetFullPath($Within).TrimEnd('\', '/')
        if (-not $full.StartsWith($boundary + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Release path must remain beneath its expected root."
        }
    }
    $current = $full
    while ($current) {
        try {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Release path contains a reparse point: $current"
            }
        } catch [IO.FileNotFoundException] {
            # A fresh output leaf is allowed; its existing parents still count.
        } catch [IO.DirectoryNotFoundException] {
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    if (-not $Recurse -or -not [IO.Directory]::Exists($full)) {
        return
    }
    if ($RejectHardLinks -and -not ('LegendCtlReleaseFileIdentity' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class LegendCtlReleaseFileIdentity {
    [StructLayout(LayoutKind.Sequential)]
    private struct Information {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Information information);
    public static uint Links(string path) {
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                   FileShare.ReadWrite | FileShare.Delete)) {
            Information information;
            if (!GetFileInformationByHandle(stream.SafeFileHandle, out information))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return information.Links;
        }
    }
}

'@
    }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($full)
    while ($pending.Count -gt 0) {
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($pending.Pop())) {
            $attributes = [IO.File]::GetAttributes($entry)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Release path contains a reparse point: $entry"
            }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                $pending.Push($entry)
            } elseif ($RejectHardLinks -and [LegendCtlReleaseFileIdentity]::Links($entry) -gt 1) {
                throw "Release mirror destination contains a hard-linked file: $entry"
            }
        }
    }
}
