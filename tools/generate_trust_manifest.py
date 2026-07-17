"""Generate the frozen-build trust record after PyInstaller has collected its payload."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_RELATIVE_PATH = Path("_internal") / "zd_app" / "trust_manifest.json"


class ManifestBuildError(RuntimeError):
    """A source or payload condition that must fail the release build closed."""


def generate_manifest(
    *,
    dist_root: str | Path,
    repo_root: str | Path,
    generated_at: datetime | None = None,
    scanner_repo_root: str | Path | None = None,
) -> Path:
    """Write one deterministic record and return its path.

    ``scanner_repo_root`` keeps the unit tests hermetic: their miniature source
    trees are scanned with this repository's real scanner implementation rather
    than a copied ruleset. The release CLI leaves it unset, so both roots are the
    actual repository being built.
    """

    repository = Path(repo_root).resolve()
    distribution = Path(dist_root).resolve()
    scanners = _load_real_scanners(
        Path(scanner_repo_root).resolve()
        if scanner_repo_root is not None
        else repository
    )
    manifest_path = distribution / _MANIFEST_RELATIVE_PATH
    document = _build_document(
        repository=repository,
        distribution=distribution,
        manifest_path=manifest_path,
        scanners=scanners,
        generated_at=generated_at,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_path, document)
    return manifest_path


def _build_document(
    *,
    repository: Path,
    distribution: Path,
    manifest_path: Path,
    scanners: dict[str, Any],
    generated_at: datetime | None,
) -> dict[str, object]:
    if not distribution.is_dir():
        raise ManifestBuildError(f"distribution root is missing: {distribution}")

    package_root = repository / "zd_app"
    entry_module = repository / "main_zd.py"
    scan = scanners["collect_static_scan"](package_root, entry_module)
    integrity = scan.integrity
    network_findings = scanners["scan_network_imports"](package_root, _scan=scan)
    browser_handoffs = scanners["scan_browser_handoffs"](package_root, _scan=scan)
    driver_findings = scanners["scan_driver_footprint"](package_root, _scan=scan)

    if not integrity.readable:
        raise ManifestBuildError("source package tree is unreadable")
    if integrity.parse_failures:
        raise ManifestBuildError(
            "source parse failures: " + ", ".join(integrity.parse_failures)
        )
    if integrity.python_file_count == 0:
        raise ManifestBuildError("source scan found no Python files")
    if not integrity.entry_module_scanned:
        raise ManifestBuildError("source scan did not include main_zd.py")
    if network_findings:
        raise ManifestBuildError("network import findings are not permitted")
    if driver_findings:
        raise ManifestBuildError("driver or virtual-device findings are not permitted")

    build_commit = _git(repository, "rev-parse", "HEAD")
    if not _is_hex(build_commit, length=40):
        raise ManifestBuildError("git rev-parse HEAD did not return a full commit SHA")
    build_commit_short = _git(repository, "rev-parse", "--short", "HEAD")
    version, patched_commit = _version_metadata(repository / "zd_app" / "version.py")
    if not patched_commit or patched_commit != build_commit_short:
        raise ManifestBuildError(
            "zd_app/version.py build commit does not match git rev-parse --short HEAD"
        )

    source_files = _source_files(scan.files, repository)
    payload_files = _payload_files(distribution, manifest_path)
    moment = generated_at or datetime.now(timezone.utc)
    return {
        "schema": 1,
        "version": version,
        "build_commit": build_commit,
        "build_commit_short": build_commit_short,
        "build_date": moment.astimezone().date().isoformat(),
        "generated_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "source_scan": {
            "ruleset": {
                "network_roots": list(scanners["network_import_roots"]),
                "driver_suffixes": list(scanners["driver_artifact_suffixes"]),
                "virtual_device_tokens": list(scanners["virtual_device_name_tokens"]),
            },
            "python_file_count": integrity.python_file_count,
            "parse_failures": list(integrity.parse_failures),
            "entry_module_scanned": integrity.entry_module_scanned,
            "network_import_findings": [
                {
                    "relative_path": finding.relative_path,
                    "line": finding.line,
                    "module": finding.module,
                }
                for finding in network_findings
            ],
            "browser_handoffs": [
                {
                    "relative_path": handoff.relative_path,
                    "line": handoff.line,
                    "call": handoff.call,
                }
                for handoff in browser_handoffs
            ],
            "driver_footprint_findings": [
                {
                    "relative_path": finding.relative_path,
                    "reason": finding.reason,
                }
                for finding in driver_findings
            ],
            "source_files": source_files,
        },
        "payload_files": payload_files,
    }


def _load_real_scanners(repo_root: Path) -> dict[str, Any]:
    # The scanners must come from the code being built; this avoids a release
    # helper silently drifting from the in-app scan rules.
    sys.path.insert(0, str(repo_root))
    from zd_app.services.trust_self_check import (  # noqa: PLC0415
        DRIVER_ARTIFACT_SUFFIXES,
        NETWORK_IMPORT_ROOTS,
        VIRTUAL_DEVICE_NAME_TOKENS,
        _collect_static_scan,
        scan_browser_handoffs,
        scan_driver_footprint,
        scan_network_imports,
    )

    return {
        "collect_static_scan": _collect_static_scan,
        "scan_network_imports": scan_network_imports,
        "scan_browser_handoffs": scan_browser_handoffs,
        "scan_driver_footprint": scan_driver_footprint,
        "network_import_roots": NETWORK_IMPORT_ROOTS,
        "driver_artifact_suffixes": DRIVER_ARTIFACT_SUFFIXES,
        "virtual_device_name_tokens": VIRTUAL_DEVICE_NAME_TOKENS,
    }


def _source_files(scan_files: tuple[object, ...], repository: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for source in scan_files:
        path = source.path
        if path.suffix.lower() != ".py":
            continue
        try:
            relative_path = path.relative_to(repository).as_posix()
        except ValueError as exc:
            raise ManifestBuildError(f"source file escaped repository: {path}") from exc
        files[relative_path] = _sha256_file(path)
    if not files:
        raise ManifestBuildError("source scan produced no hashable Python files")
    return dict(sorted(files.items()))


def _payload_files(distribution: Path, manifest_path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        payload_paths = tuple(path for path in distribution.rglob("*") if path.is_file())
    except OSError as exc:
        raise ManifestBuildError(f"payload tree is unreadable: {exc}") from exc
    for path in sorted(payload_paths):
        if path == manifest_path:
            continue
        try:
            relative_path = path.relative_to(distribution).as_posix()
        except ValueError as exc:
            raise ManifestBuildError(f"payload file escaped distribution: {path}") from exc
        files[relative_path] = _sha256_file(path)
    if not files:
        raise ManifestBuildError("payload tree has no files to hash")
    return dict(sorted(files.items()))


def _version_metadata(version_path: Path) -> tuple[str, str]:
    try:
        tree = ast.parse(version_path.read_text(encoding="utf-8"), filename=str(version_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ManifestBuildError(f"cannot read version metadata: {exc}") from exc
    values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in {
                "__version__",
                "__build_commit__",
            }:
                continue
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, str):
                values[target.id] = value
    version = values.get("__version__")
    if not version:
        raise ManifestBuildError("zd_app/version.py does not define __version__")
    return version, values.get("__build_commit__", "")


def _git(repository: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestBuildError(f"git {' '.join(args)} failed: {exc}") from exc
    return result.stdout.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestBuildError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _is_hex(value: str, *, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _write_json(path: Path, document: dict[str, object]) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        generate_manifest(dist_root=args.dist_root, repo_root=args.repo_root)
    except ManifestBuildError as exc:
        print(f"trust manifest failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
