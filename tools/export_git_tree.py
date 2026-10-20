"""Commit-bound local release source and non-destructive artifact handling.

No checkout, archive attributes, filters, hooks or replace refs participate in
the export. The existing release venv remains a separately trusted toolchain.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile


class ExportError(RuntimeError):
    """The requested source or destination cannot support provenance."""


# Excluded, never copied or imported. These allowances apply only to untracked
# files; a tracked file is always compared with its committed bytes.
GENERATED_ROOTS = {".venv-zd", "build", "dist", "local-install", "zd_data", ".pytest_cache",
                   ".mypy_cache", ".ruff_cache", ".release-wheelhouse", ".release-tools"}


@functools.lru_cache(maxsize=None)
def trusted_git(expected: str | None = None) -> str:
    """Authenticate a protected installation without consulting PATH or Git."""
    if os.name == "nt":
        system = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)):
            raise ctypes.WinError()
        powershell = Path(system.value) / "WindowsPowerShell/v1.0/powershell.exe"
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-File",
                   str(Path(__file__).with_name("release_git.ps1"))]
        if expected:
            command += ["-ExpectedPath", expected]
        result = subprocess.run(command, capture_output=True, env={
            key: value for key, value in _git_env().items() if key.upper() != "PSMODULEPATH"})
        if result.returncode:
            detail = " ".join(result.stderr.decode(errors="replace").split())[:2000]
            raise ExportError(f"Trusted release Git authentication failed (exit {result.returncode}): {detail}")
        candidate = result.stdout.decode("utf-8-sig").strip()
        if not Path(candidate).is_absolute():
            raise ExportError("Trusted release Git did not return an absolute path")
        return candidate
    # Source-only checks also run on hosted Linux. Use a root-owned system Git;
    # there is no test flag, PATH fallback or unprotected executable override.
    candidate = Path("/usr/bin/git").resolve(strict=True)
    if expected and Path(expected) != candidate:
        raise ExportError("Release Git must be the protected system executable")
    for item in (candidate, *candidate.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ExportError("Release Git system installation is writable by an unprivileged user")
    return str(candidate)


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL=":")
    return env


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run([trusted_git(), "--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false",
                             "-c", f"core.hooksPath={os.devnull}", "-c", f"core.attributesFile={os.devnull}",
                             "-c", "core.untrackedCache=false", "-c", "protocol.allow=never",
                             "-C", str(repo), *args],
                            input=data, capture_output=True, env=_git_env())
    if result.returncode:
        raise ExportError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _absolute(path: Path) -> Path:
    # Do not resolve first: doing so hides the very junction we must reject.
    absolute = Path(os.path.abspath(path))
    if os.name == "nt" and (str(absolute).startswith("\\\\") or ":" in str(absolute)[2:]):
        raise ExportError("Release paths must be local drive paths without alternate data streams")
    return absolute


def _ordinary(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ExportError(f"Reparse point or symlink is forbidden: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ExportError(f"Not an ordinary file or directory: {path}")
    return info


class PathGuard:
    """Remember path identities; on Windows deny directory deletion/replacement.

    Locks cover every ancestor, including the workspace/output parent, while
    children are created or used. No recursive removal is performed anywhere.
    This is not a sandbox against another process with the same account rights.
    """
    def __init__(self) -> None:
        self.identities: dict[Path, tuple[int, int]] = {}
        self.handles: list[int] = []

    def __enter__(self) -> PathGuard:
        return self

    def __exit__(self, *_: object) -> None:
        if os.name == "nt":
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [ctypes.c_void_p]
            for handle in reversed(self.handles):
                close(handle)

    def check(self) -> None:
        for path, identity in self.identities.items():
            info = _ordinary(path)
            if (info.st_dev, info.st_ino) != identity:
                raise ExportError(f"Path was replaced during release: {path}")

    def add(self, path: Path) -> None:
        path = _absolute(path)
        for current in reversed((path, *path.parents)):
            if current in self.identities:
                continue
            info = _ordinary(current)
            if not stat.S_ISDIR(info.st_mode):
                raise ExportError(f"Expected directory: {current}")
            if os.name == "nt":
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                create = api.CreateFileW
                create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
                create.restype = ctypes.c_void_p
                # GENERIC_READ, SHARE_READ|WRITE (no SHARE_DELETE),
                # OPEN_EXISTING, BACKUP_SEMANTICS|OPEN_REPARSE_POINT.
                handle = create(str(current), 0x80000000, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
            self.identities[current] = (info.st_dev, info.st_ino)
        self.check()


def _safe_destination(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if (PurePosixPath(relative).is_absolute() or any(
        not part or part in {".", ".."} or part.lower() == ".git"
        or part.endswith((".", " ")) or any(c in part for c in '\\:<>"|?*')
        or any(ord(c) < 32 for c in part) or part.split(".")[0].upper() in reserved
        for part in parts
    )):
        raise ExportError(f"Unsafe Git/output path: {relative!r}")
    return root.joinpath(*parts)


def _tree_entries(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    entries = []
    seen: dict[str, str] = {}
    for record in _git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        relative = raw_path.decode("utf-8")
        _safe_destination(repo, relative)
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ExportError(f"Unsupported Git entry: {relative} ({mode} {kind})")
        # Also reject directory aliases (A/one and a/two) on Windows.
        for i in range(1, len(relative.split("/")) + 1):
            prefix = "/".join(relative.split("/")[:i])
            key = prefix.casefold()
            if key in seen and seen[key] != prefix:
                raise ExportError(f"Case-colliding Git path: {relative}")
            seen[key] = prefix
        entries.append((mode, oid, relative))
    return entries


def _blobs(repo: Path, entries: list[tuple[str, str, str]]):
    # communicate drains both pipes, avoiding an stderr/full-pipe deadlock.
    payload = _git(repo, "cat-file", "--batch",
                   data=b"".join(oid.encode() + b"\n" for _, oid, _ in entries))
    offset = 0
    for mode, oid, relative in entries:
        end = payload.index(b"\n", offset)
        header = payload[offset:end].decode("ascii").split()
        if len(header) != 3:
            raise ExportError(f"Raw Git blob unavailable: {relative}")
        actual, kind, size = header
        size = int(size)
        start = end + 1
        blob = payload[start:start + size]
        if actual != oid or kind != "blob" or len(blob) != size or payload[start + size:start + size + 1] != b"\n":
            raise ExportError(f"Invalid raw Git blob: {relative}")
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        if hashlib.new(algorithm, f"blob {size}\0".encode() + blob).hexdigest() != oid:
            raise ExportError(f"Raw Git blob hash mismatch: {relative}")
        yield mode, oid, relative, blob
        offset = start + size + 1


def _allowed_generated(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (path.parts[0] in GENERATED_ROOTS
            or ("__pycache__" in path.parts[:-1] and path.suffix == ".pyc"))


def _bytecode_cache(path: Path) -> bool:
    """Inspect only ordinary cache entries; never follow a directory link."""
    info = _ordinary(path)
    if stat.S_ISDIR(info.st_mode):
        return all(_bytecode_cache(child) for child in path.iterdir())
    return path.suffix == ".pyc"


def assert_clean_tree(repo: Path) -> str:
    repo = _absolute(repo)
    with PathGuard() as guard:
        guard.add(repo)
        top = _absolute(Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()))
        # Git expands Windows 8.3 names (including hosted runner TEMP paths).
        # Validate both spellings before comparing filesystem identity: resolving
        # first would hide a junction, while lexical equality rejects safe aliases.
        guard.add(top)
        if not repo.samefile(top):
            raise ExportError("Release source must be the Git worktree root")
        commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        flags = _git(repo, "ls-files", "-v", "-z").split(b"\0")
        if any(flag and not flag.startswith(b"H ") for flag in flags):
            raise ExportError("Exceptional index flags (assume-unchanged/skip-worktree) are forbidden")
        if _git(repo, "diff-index", "--cached", "--name-only", "-z", commit, "--"):
            raise ExportError("Staged source changes are forbidden")
        entries = _tree_entries(repo, commit)
        paths = b"".join(relative.encode() + b"\0" for _, _, relative in entries)
        attrs = _git(repo, "check-attr", "--cached", "-z", "--stdin", "text", "eol", data=paths).split(b"\0")
        attributes = {(attrs[i].decode(), attrs[i + 1].decode()): attrs[i + 2].decode()
                      for i in range(0, len(attrs) - 1, 3)}
        config = _git(repo, "config", "--list")
        for _, _, relative, blob in _blobs(repo, entries):
            path = _safe_destination(repo, relative)
            guard.add(path.parent)
            info = _ordinary(path)
            if not stat.S_ISREG(info.st_mode):
                raise ExportError(f"Tracked source is not a file: {relative}")
            current = path.read_bytes()
            text = attributes.get((relative, "text"))
            eol = attributes.get((relative, "eol"))
            newline_checkout = (text != "unset" and b"\0" not in blob and
                                (text in {"set", "auto"} or eol in {"lf", "crlf"}
                                 or b"core.autocrlf=true" in config or b"core.autocrlf=input" in config))
            if current != blob and not (newline_checkout and current.replace(b"\r\n", b"\n") == blob):
                raise ExportError(f"Dirty tracked source: {relative}")
        # Collapse whole untracked directories: do not walk old build outputs,
        # release venvs or junctions nested inside those excluded directories.
        others = _git(repo, "ls-files", "--others", "--directory", "--exclude-standard", "-z")
        others += _git(repo, "ls-files", "--others", "--directory", "--ignored", "--exclude-standard", "-z")
        for raw in others.split(b"\0"):
            if raw:
                relative = raw.decode("utf-8")
                cache = PurePosixPath(relative).name == "__pycache__" and _bytecode_cache(repo / relative)
                if not _allowed_generated(relative) and not cache:
                    raise ExportError(f"Unsafe untracked/ignored input: {relative}")
        # Generated roots cannot replace paths with junctions, even though their
        # bytes are never exported. Reject links without traversing their targets.
        for root in GENERATED_ROOTS:
            candidate = repo / root
            if candidate.exists() or candidate.is_symlink():
                _ordinary(candidate)
        guard.check()
        if _git(repo, "rev-parse", "HEAD").decode().strip() != commit:
            raise ExportError("HEAD changed during source admission")
        return commit


def export_tree(repo: Path, commit: str, destination: Path, guard: PathGuard) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ExportError("Export requires an immutable full commit ID")
    if _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() != commit:
        raise ExportError("Export identity does not match the commit object")
    guard.add(destination)
    if any(destination.iterdir()):
        raise ExportError("Source destination must be empty")
    manifest = {}
    for mode, _, relative, blob in _blobs(repo, _tree_entries(repo, commit)):
        guard.check()
        output = _safe_destination(destination, relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        guard.add(output.parent)
        with output.open("xb") as stream:
            stream.write(blob)
        if mode == "100755":
            output.chmod(output.stat().st_mode | 0o111)
        manifest[relative] = hashlib.sha256(blob).hexdigest()
    return manifest


def verify_inventory(source: Path, manifest: dict[str, str], *, after_build: bool = False) -> None:
    """Refuse extra inputs, including empty directories and ignored files."""
    expected_directories = {parent.as_posix() for name in manifest
                            for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    pending = [source]
    while pending:
        for path in pending.pop().iterdir():
            info = _ordinary(path)
            relative = path.relative_to(source).as_posix()
            if after_build and relative in {"build", "dist"} and stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                if relative not in expected_directories:
                    raise ExportError(f"Unexpected exported source directory: {relative}")
                pending.append(path)
            elif info.st_nlink != 1:
                raise ExportError(f"Hardlinked exported source is forbidden: {relative}")
            elif relative not in manifest:
                raise ExportError(f"Unexpected exported source file: {relative}")


def verify_snapshot(source: Path, manifest: dict[str, str], *, exact: bool = True) -> None:
    if exact:
        verify_inventory(source, manifest)
    for relative, expected in manifest.items():
        path = _safe_destination(source, relative)
        for parent in path.parents:
            _ordinary(parent)
            if parent == source:
                break
        _ordinary(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ExportError(f"Exported source changed: {relative}")


def verify_source(repo: Path, source: Path, commit: str) -> None:
    """Bind the direct worker's complete raw tree to an immutable Git commit."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ExportError("Worker requires an immutable full commit ID")
    repo, source = _absolute(repo), _absolute(source)
    with PathGuard() as guard:
        guard.add(repo)
        guard.add(source)
        if _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() != commit:
            raise ExportError("Worker identity does not match the commit object")
        manifest = {relative: hashlib.sha256(blob).hexdigest()
                    for _, _, relative, blob in _blobs(repo, _tree_entries(repo, commit))}
        verify_snapshot(source, manifest)
        guard.check()


def _output_files(root: Path, guard: PathGuard):
    guard.add(root)
    for entry in root.iterdir():
        info = _ordinary(entry)
        if stat.S_ISDIR(info.st_mode):
            yield from _output_files(entry, guard)
        elif info.st_nlink != 1:
            raise ExportError(f"Hardlinked artifact is forbidden: {entry.name}")
        else:
            yield entry


def verify_build_identity(source: Path, commit: str, manifest: dict[str, str],
                          raw_version: bytes | None = None) -> str:
    """Only version metadata may differ from the raw commit after packaging."""
    verify_inventory(source, manifest, after_build=True)
    unchanged = {name: digest for name, digest in manifest.items() if name != "zd_app/version.py"}
    verify_snapshot(source, unchanged, exact=False)
    version_path = source / "zd_app" / "version.py"
    _ordinary(version_path)
    tree = ast.parse(version_path.read_text(encoding="utf-8-sig"))
    labels = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "__build_commit__" for t in node.targets)
              and isinstance(node.value, ast.Constant)]
    if labels != [commit]:
        raise ExportError("Artifact build label does not match exported source identity")
    if raw_version is not None:
        def program_without_build_metadata(parsed):
            parsed.body = [node for node in parsed.body if not (
                isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in {"__build_commit__", "__build_date__"}
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
            return ast.dump(parsed)
        if program_without_build_metadata(tree) != program_without_build_metadata(ast.parse(raw_version.decode("utf-8-sig"))):
            raise ExportError("Version source changed beyond build metadata")
    return hashlib.sha256(version_path.read_bytes()).hexdigest()


def publish_artifacts(source: Path, destination: Path, commit: str, guard: PathGuard,
                      source_manifest: dict[str, str] | None = None,
                      version_hash: str | None = None) -> None:
    guard.add(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ExportError("Artifact destination already exists; choose a new destination")
    files = list(_output_files(source, guard))
    if not (source / "SHA256SUMS.txt").is_file():
        raise ExportError("Build did not produce SHA256SUMS.txt")
    artifacts = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in files}
    if "SOURCE_PROVENANCE.json" in artifacts:
        raise ExportError("Build output already contains a provenance receipt")
    for line in (source / "SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or artifacts.get(name) != digest.lower():
            raise ExportError("SHA256SUMS.txt does not match the produced artifacts")
    if not any(name.endswith("-windows.zip") for name in artifacts):
        raise ExportError("Build did not produce a portable ZIP")
    guard.check()
    destination.mkdir()
    guard.add(destination)
    for path in files:
        guard.check()
        output = _safe_destination(destination, path.relative_to(source).as_posix())
        output.parent.mkdir(parents=True, exist_ok=True)
        guard.add(output.parent)
        _ordinary(path)
        with path.open("rb") as incoming, output.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if hashlib.sha256(output.read_bytes()).hexdigest() != artifacts[path.relative_to(source).as_posix()]:
            raise ExportError("Artifact changed during publication")
    guard.check()
    # Last operation: partial output on any earlier failure has no completion
    # receipt and the caller reports failure, never "Build complete".
    with (destination / "SOURCE_PROVENANCE.json").open("x", encoding="utf-8") as stream:
        json.dump({"source_commit": commit, "build_commit": commit, "complete": True,
                   "raw_source_sha256": source_manifest or {}, "stamped_version_sha256": version_hash,
                   "artifact_sha256": artifacts}, stream, indent=2, sort_keys=True)


def run_build(repo: Path, destination: Path, python: Path) -> None:
    repo, destination = _absolute(repo), _absolute(destination)
    with PathGuard() as guard:
        guard.add(repo)
        guard.add(destination.parent)
        if repo in destination.parents and destination.parent != repo:
            raise ExportError("Artifacts inside the checkout must use a new top-level output directory")
        if destination == repo or destination in repo.parents or destination.exists() or destination.is_symlink():
            raise ExportError("Artifact destination must be new and cannot replace source")
        commit = assert_clean_tree(repo)
        # A unique source remains on disk on both success and failure for
        # diagnostics. Never recursively delete a possibly replaced user path.
        source = Path(tempfile.mkdtemp(prefix="legendctl-release-source-"))
        print(f"Isolated release source (retained for diagnostics): {source}", flush=True)
        manifest = export_tree(repo, commit, source, guard)
        verify_snapshot(source, manifest)
        guard.check()
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("PYTHON", "PIP_", "GIT_")) and k.upper() != "PSMODULEPATH"}
        env["PIP_CONFIG_FILE"] = os.devnull
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(source / "tools" / "build_release.ps1"), "-FromVerifiedSnapshot",
            "-BuildPython", str(python), "-ExpectedCommit", commit,
            "-SourceRepository", str(repo), "-GitExecutable", trusted_git(),
            "-InnoToolchainRoot", str(repo / ".release-tools"),
        ], cwd=source, env=env)
        if result.returncode:
            raise ExportError(f"Isolated release build failed (exit {result.returncode}); source retained: {source}")
        guard.check()
        version_hash = verify_build_identity(source, commit, manifest,
                                            _git(repo, "cat-file", "blob", f"{commit}:zd_app/version.py"))
        list(_output_files(source / "dist", guard))
        trust_paths = list((source / "dist").glob("ZDUltimateLegend-v*/_internal/zd_app/trust_manifest.json"))
        if len(trust_paths) != 1:
            raise ExportError("Expected exactly one packaged trust manifest")
        _ordinary(trust_paths[0])
        trust = json.loads(trust_paths[0].read_text(encoding="utf-8"))
        if trust.get("build_commit") != commit or trust.get("build_commit_short") != commit:
            raise ExportError("Packaged trust manifest does not match exported source identity")
        publish_artifacts(source / "dist", destination, commit, guard, manifest, version_hash)
        print(f"Build complete: {destination} (source {commit})", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--git", help="Expected authenticated system Git executable")
    parser.add_argument("--verify-source", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.git:
        trusted_git(args.git)
    if args.verify_source:
        if not args.commit:
            parser.error("source verification requires --commit")
        verify_source(args.repo, args.verify_source, args.commit)
    elif args.check:
        print(assert_clean_tree(args.repo))
    else:
        if not args.destination or not args.python:
            parser.error("build requires --destination and --python")
        run_build(args.repo, args.destination, args.python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
