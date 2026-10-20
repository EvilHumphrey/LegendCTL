// Exact local mirroring without following redirects or overwriting old file
// objects. A failure can leave a partial install; rerunning repairs it.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public sealed class LegendCtlReleaseMirror : IDisposable {
    const uint Read = 0x80000000, Write = 0x40000000, Delete = 0x10000;
    readonly string source, destination;
    readonly Dictionary<string, SafeFileHandle> pins =
        new Dictionary<string, SafeFileHandle>(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> sourceFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> sourceDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> destinationFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> destinationDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    bool prepared;

    [StructLayout(LayoutKind.Sequential)]
    struct Information {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Information information);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,
        System.Text.StringBuilder path, uint length, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint GetLongPathNameW(string path, System.Text.StringBuilder expanded, uint length);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int kind,
        ref byte information, uint size);

    static IOException Error(string action) {
        return new IOException(action, new Win32Exception(Marshal.GetLastWin32Error()));
    }
    static string LocalPath(string path) {
        if (path.Length < 3 || !char.IsLetter(path[0]) || path[1] != ':' ||
            (path[2] != '\\' && path[2] != '/') || path.Substring(2).Contains(":"))
            throw new IOException("Mirror paths must be ordinary absolute local drive paths.");
        return Path.GetFullPath(path).TrimEnd('\\');
    }
    static bool Beneath(string child, string parent) {
        return child.StartsWith(parent + "\\", StringComparison.OrdinalIgnoreCase);
    }
    static SafeFileHandle Open(string path, uint access, uint creation = 3, uint share = 1) {
        // Final-component no-follow; every parent is pinned before this call.
        var handle = CreateFileW(path, access, share, IntPtr.Zero, creation, 0x02200000, IntPtr.Zero);
        if (handle.IsInvalid) {
            handle.Dispose();
            throw Error("Cannot open mirror object: " + Path.GetFileName(path));
        }
        return handle;
    }
    static void Check(SafeFileHandle handle, string path, bool directory) {
        Information info;
        if (!GetFileInformationByHandle(handle, out info))
            throw Error("Cannot inspect mirror object");
        if ((info.Attributes & 0x400) != 0)
            throw new IOException("Mirror path contains a reparse point.");
        if (((info.Attributes & 0x10) != 0) != directory)
            throw new IOException("Mirror object type changed.");
        if (!directory && info.Links != 1)
            throw new IOException("Mirror contains a hard-linked file.");
        var buffer = new System.Text.StringBuilder(32768);
        var length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity)
            throw Error("Cannot establish physical mirror path");
        string actual = buffer.ToString();
        if (actual.StartsWith(@"\\?\")) actual = actual.Substring(4);
        var expanded = new System.Text.StringBuilder(32768);
        uint expandedLength = GetLongPathNameW(path, expanded, (uint)expanded.Capacity);
        if (expandedLength == 0 || expandedLength >= expanded.Capacity)
            throw Error("Cannot expand physical mirror path");
        // Existing parents are pinned and the opened leaf has already passed
        // the no-reparse check. Expanding 8.3 spelling here preserves TEMP paths
        // used by hosted Windows without resolving away an unchecked redirect.
        if (!actual.TrimEnd('\\').Equals(expanded.ToString().TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new IOException("Physical mirror path differs from the expected path.");
    }
    void Pin(string path, bool deletable = false) {
        if (pins.ContainsKey(path)) return;
        var handle = Open(path, Read | (deletable ? Delete : 0));
        try { Check(handle, path, true); }
        catch { handle.Dispose(); throw; }
        pins.Add(path, handle);
    }
    void PinAncestors(string path) {
        var ancestors = new Stack<string>();
        for (var current = path; current != null; current = Path.GetDirectoryName(current))
            ancestors.Push(current);
        while (ancestors.Count != 0) Pin(ancestors.Pop());
    }
    static void Retire(SafeFileHandle handle) {
        byte delete = 1;
        if (!SetFileInformationByHandle(handle, 4, ref delete, 1))
            throw Error("Cannot retire mirror object by handle");
    }
    void Inventory(string root, HashSet<string> files, HashSet<string> dirs, bool deletable) {
        var pending = new Stack<string>();
        pending.Push(root);
        while (pending.Count != 0) {
            var current = pending.Pop();
            foreach (var path in Directory.EnumerateFileSystemEntries(current)) {
                var relative = path.Substring(root.Length + 1);
                var attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Mirror path contains a reparse point.");
                if ((attributes & FileAttributes.Directory) != 0) {
                    Pin(path, deletable);
                    dirs.Add(relative);
                    pending.Push(path);
                } else {
                    using (var handle = Open(path, Read)) Check(handle, path, false);
                    files.Add(relative);
                }
            }
        }
    }
    public LegendCtlReleaseMirror(string sourcePath, string destinationPath) {
        source = LocalPath(sourcePath);
        destination = LocalPath(destinationPath);
        if (source.Equals(destination, StringComparison.OrdinalIgnoreCase) ||
            Beneath(source, destination) || Beneath(destination, source))
            throw new IOException("Mirror source and destination must be disjoint.");
    }
    public void Prepare() {
        if (prepared) throw new InvalidOperationException("Mirror is already prepared.");
        PinAncestors(source);
        PinAncestors(Path.GetDirectoryName(destination));
        Directory.CreateDirectory(destination);
        Pin(destination);
        Inventory(source, sourceFiles, sourceDirs, false);
        Inventory(destination, destinationFiles, destinationDirs, true);
        prepared = true;
    }
    void DeleteFile(string path) {
        using (var handle = Open(path, Read | Delete, 3, 0)) {
            Check(handle, path, false);
            Retire(handle);
        }
    }
    public void Publish() {
        if (!prepared) throw new InvalidOperationException("Prepare the mirror first.");
        foreach (var relative in destinationFiles)
            if (!sourceFiles.Contains(relative)) DeleteFile(Path.Combine(destination, relative));
        foreach (var relative in destinationDirs.OrderByDescending(path => path.Length)) {
            if (sourceDirs.Contains(relative)) continue;
            string path = Path.Combine(destination, relative);
            var handle = pins[path];
            Check(handle, path, true);
            // Nonempty or newly populated directories fail closed. No traversal.
            Retire(handle);
            handle.Dispose();
            pins.Remove(path);
        }
        foreach (var relative in sourceDirs.OrderBy(path => path.Length)) {
            string path = Path.Combine(destination, relative);
            Directory.CreateDirectory(path);
            Pin(path, true);
        }
        foreach (var relative in sourceFiles) {
            string from = Path.Combine(source, relative), to = Path.Combine(destination, relative);
            using (var input = Open(from, Read)) {
                Check(input, from, false);
                // Never truncate an existing object: a hardlink created after
                // validation retains the old object's bytes when it is retired.
                if (destinationFiles.Contains(relative)) DeleteFile(to);
                using (var output = Open(to, Read | Write | Delete, 1, 0)) {
                    Check(output, to, false);
                    using (var reader = new FileStream(input, FileAccess.Read))
                    using (var writer = new FileStream(output, FileAccess.Write)) {
                        reader.CopyTo(writer);
                        writer.Flush(true);
                    }
                }
            }
        }
        // Refuse late extras instead of treating an interrupted/concurrent mirror
        // as a successful exact install. No newly discovered object is removed.
        var actualFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var actualDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        Inventory(destination, actualFiles, actualDirs, true);
        if (!actualFiles.SetEquals(sourceFiles) || !actualDirs.SetEquals(sourceDirs))
            throw new IOException("Mirror inventory changed during publication.");
    }
    public void Dispose() {
        foreach (var handle in pins.Values.Reverse()) handle.Dispose();
        pins.Clear();
    }
}
