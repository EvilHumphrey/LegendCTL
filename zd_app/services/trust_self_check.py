"""Trust self-check evidence for the Diagnostics screen."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping

from zd_app import version as app_version
from zd_app.i18n import t
from zd_app.services.markdown_safety import escape_markdown
from zd_app.services.model_fingerprint import ModelFingerprint, fingerprint_display_rows
from zd_app.services.path_scrub import sanitize_shareable_text, scrub_paths
from zd_app.storage._import_guards import read_guarded_json
from zd_app.storage.settings_store import _default_user_data_dir


NETWORK_IMPORT_ROOTS = ("socket", "http", "urllib", "requests", "ssl")
DRIVER_ARTIFACT_SUFFIXES = (".sys", ".inf")
VIRTUAL_DEVICE_NAME_TOKENS = ("vigem", "virtualhid", "vhid", "hidguardian")
BOUNDARY_TEXT_KEY = "trust_self_check.boundary.session"
TRUST_MANIFEST_FILENAME = "trust_manifest.json"
TRUST_MANIFEST_SCHEMA = 1
MAX_TRUST_MANIFEST_BYTES = 1 * 1024 * 1024
_PATH_ELLIPSIS = "\u2026"


@dataclass(frozen=True)
class StaticImportFinding:
    relative_path: str
    line: int
    module: str


@dataclass(frozen=True)
class BrowserHandoff:
    relative_path: str
    line: int
    call: str


@dataclass(frozen=True)
class FootprintFinding:
    relative_path: str
    reason: str


@dataclass(frozen=True)
class ScanIntegrity:
    readable: bool
    python_file_count: int
    parse_failures: tuple[str, ...]
    entry_module_scanned: bool
    # Files parsed from the walked package tree plus the entry module -- the
    # scope the user-visible evidence sentences NAME. ``python_file_count`` is
    # the whole parsed set, which also includes first-party modules imported
    # from outside the package tree. The two differ on purpose; see
    # ``_network_evidence``.
    named_scope_python_file_count: int = 0
    # The same idea one notch narrower: files parsed from the package walk
    # ALONE. The driver sentence names only the package directory, so it may
    # not report a number that counts the entry module sitting beside it. See
    # ``_driver_evidence``.
    named_scope_package_file_count: int = 0


@dataclass(frozen=True)
class _ScanPath:
    path: Path
    relative_path: str


@dataclass(frozen=True)
class _PackageFiles:
    readable: bool
    files: tuple[Path, ...]


@dataclass(frozen=True)
class _StaticScan:
    files: tuple[_ScanPath, ...]
    parsed_python_files: tuple[tuple[_ScanPath, ast.AST], ...]
    integrity: ScanIntegrity


@dataclass(frozen=True)
class _ImportClosure:
    files: tuple[tuple[_ScanPath, ast.AST | None], ...]
    # False when the walk was cut short by a filesystem refusal, so the caller
    # can tell "nothing to add" apart from "could not look".
    complete: bool


@dataclass(frozen=True)
class ManifestIntegrity:
    """Whether a packaged build record was present and safe to use."""

    present: bool
    valid: bool
    reason: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustManifest:
    """A parsed build-time record carried alongside a frozen payload."""

    integrity: ManifestIntegrity
    manifest_path: Path
    version: str = ""
    build_commit: str = ""
    build_commit_short: str = ""
    build_date: str = ""
    generated_at: str = ""
    source_scan: Mapping[str, object] = field(default_factory=dict)
    payload_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PayloadVerification:
    """Hashes observed from the current frozen payload, excluding its EXE."""

    matched: int
    total: int
    mismatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    extra_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.mismatched and not self.missing


@dataclass(frozen=True)
class TrustSelfCheckRow:
    key: str
    claim: str
    evidence: str
    boundary: str


@dataclass(frozen=True)
class TrustSelfCheckResult:
    generated_at: str
    version: str
    build_commit: str
    build_date: str
    run_mode: str
    executable_path: str
    data_dir: str
    package_file_count: int
    scan_integrity: ScanIntegrity
    network_import_findings: tuple[StaticImportFinding, ...]
    browser_handoffs: tuple[BrowserHandoff, ...]
    footprint_findings: tuple[FootprintFinding, ...]
    rows: tuple[TrustSelfCheckRow, ...]
    model_fingerprint: ModelFingerprint | None = None
    manifest: TrustManifest | None = None
    payload_verification: PayloadVerification | None = None

    @property
    def manifest_present(self) -> bool:
        return self.manifest is not None and self.manifest.integrity.present

    @property
    def manifest_valid(self) -> bool:
        return self.manifest is not None and self.manifest.integrity.valid

    def to_markdown(self) -> str:
        """Render the copy-pasteable, Markdown-safe self-check artifact."""

        lines = [
            f"# {_md(t('trust_self_check.title'))}",
            "",
            _md(t("trust_self_check.intro")),
            "",
            f"- {_md(t('trust_self_check.generated_label'))}: {_md(self.generated_at)}",
            f"- {_md(t('trust_self_check.version_label'))}: {_md(self.version)}",
            f"- {_md(t('trust_self_check.commit_label'))}: {_md(self.build_commit)}",
            f"- {_md(t('trust_self_check.build_date_label'))}: {_md(self.build_date)}",
            f"- {_md(t('trust_self_check.run_mode_label'))}: {_md(self.run_mode)}",
            "",
            "| Claim | Evidence | Boundary |",
            "| --- | --- | --- |",
        ]
        for row in self.rows:
            lines.append(
                f"| {_md(row.claim)} | {_md(row.evidence)} | {_md(row.boundary)} |"
            )
        if self.model_fingerprint is not None:
            lines.extend(("", f"## {_md(t('model_fingerprint.title'))}"))
            for label, value in _model_fingerprint_rows(self.model_fingerprint):
                lines.append(f"- {_md(label)}: {_md(value)}")
            lines.append(
                f"- {_md(t('model_fingerprint.write_validation_basis_label'))}: "
                f"{_md(t('model_fingerprint.write_validation_basis_value'))}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def to_text(self) -> str:
        """Render a plain-text equivalent for tests and non-Markdown sinks."""

        lines = [
            t("trust_self_check.title"),
            t("trust_self_check.intro"),
            "",
            f"{t('trust_self_check.generated_label')}: {self.generated_at}",
            f"{t('trust_self_check.version_label')}: {self.version}",
            f"{t('trust_self_check.commit_label')}: {self.build_commit}",
            f"{t('trust_self_check.build_date_label')}: {self.build_date}",
            f"{t('trust_self_check.run_mode_label')}: {self.run_mode}",
            "",
        ]
        for row in self.rows:
            lines.extend(
                (
                    row.claim,
                    f"  {row.evidence}",
                    f"  {row.boundary}",
                    "",
                )
            )
        if self.model_fingerprint is not None:
            lines.extend(("", t("model_fingerprint.title")))
            for label, value in _model_fingerprint_rows(self.model_fingerprint):
                lines.append(f"{label}: {value}")
            lines.append(
                f"{t('model_fingerprint.write_validation_basis_label')}: "
                f"{t('model_fingerprint.write_validation_basis_value')}"
            )
        return "\n".join(lines).rstrip() + "\n"


def build_trust_self_check(
    *,
    package_root: str | Path | None = None,
    entry_module_path: str | Path | None = None,
    executable_path: str | Path | None = None,
    user_data_dir: str | Path | None = None,
    frozen: bool | None = None,
    now: datetime | None = None,
    model_fingerprint: ModelFingerprint | None = None,
) -> TrustSelfCheckResult:
    """Assemble the in-session trust evidence without network or device I/O."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    generated_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    raw_executable = Path(executable_path or sys.executable)
    executable = _display_path(raw_executable)
    data_dir = _display_path(user_data_dir or _default_user_data_dir())
    run_mode = _run_mode_label(is_frozen, str(executable_path or sys.executable))
    scan = _collect_static_scan(root, entry_module_path)
    scan_integrity = scan.integrity
    scan_verified = _scan_verified(scan_integrity)
    network_findings = scan_network_imports(root, _scan=scan)
    handoffs = scan_browser_handoffs(root, _scan=scan)
    footprint_findings = scan_driver_footprint(root, _scan=scan)
    version = _clean_observed_value(getattr(app_version, "__version__", ""))
    build_commit = _clean_observed_value(
        getattr(app_version, "__build_commit__", "")
    ) or t("trust_self_check.value.not_embedded")
    build_date = _clean_observed_value(
        getattr(app_version, "__build_date__", "")
    ) or t("trust_self_check.value.not_embedded")
    boundary = t(BOUNDARY_TEXT_KEY)

    # A build record is intentionally considered only in this narrow branch:
    # source/dev runs retain their live scan, and a live verified frozen scan
    # must never be replaced by build-time evidence.
    manifest: TrustManifest | None = None
    payload_verification: PayloadVerification | None = None
    manifest_reason: str | None = None
    manifest_applies = False
    if is_frozen and not scan_verified:
        manifest = load_trust_manifest(root)
        if manifest is not None:
            if not manifest.integrity.valid:
                manifest_reason = t("trust_self_check.manifest.reason.invalid")
            elif manifest.build_commit_short != getattr(
                app_version, "__build_commit__", ""
            ):
                manifest_reason = t("trust_self_check.manifest.reason.skew")
            else:
                payload_verification = verify_payload_against_manifest(
                    manifest,
                    raw_executable.parent,
                    running_executable=raw_executable,
                )
                manifest_applies = payload_verification.clean

    if manifest_applies or network_findings or scan_verified:
        network_claim = t("trust_self_check.network.claim")
    else:
        network_claim = t("trust_self_check.scan.unverified_claim.network")
    if manifest_applies or footprint_findings or scan_verified:
        driver_claim = t("trust_self_check.drivers.claim")
        driver_boundary = t("trust_self_check.drivers.boundary")
    else:
        driver_claim = t("trust_self_check.scan.unverified_claim.drivers")
        driver_boundary = boundary

    manifest_boundary = t("trust_self_check.manifest.boundary")
    if manifest_applies and manifest is not None and payload_verification is not None:
        network_evidence = _manifest_network_evidence(manifest, payload_verification)
        driver_evidence = _manifest_driver_evidence(manifest, payload_verification)
        network_boundary = manifest_boundary
        driver_boundary = manifest_boundary
    elif payload_verification is not None:
        # The source record can still be described as recorded data, but a
        # mismatch or missing payload file cannot support today's clean claim.
        warning = _payload_warning_evidence(payload_verification)
        network_evidence = warning
        driver_evidence = warning
        network_boundary = manifest_boundary
        driver_boundary = manifest_boundary
    elif manifest_reason is not None:
        network_evidence = _unverified_evidence(scan_integrity, reason=manifest_reason)
        driver_evidence = _unverified_evidence(scan_integrity, reason=manifest_reason)
        network_boundary = boundary
    else:
        network_evidence = _network_evidence(
            network_findings,
            handoffs,
            scan_integrity=scan_integrity,
        )
        driver_evidence = _driver_evidence(
            footprint_findings,
            scan_integrity=scan_integrity,
        )
        network_boundary = boundary

    build_evidence = t(
        "trust_self_check.build.evidence",
        version=version,
        commit=build_commit,
        date=build_date,
        run_mode=run_mode,
    )
    if manifest_applies and manifest is not None:
        recorded_exe_hash = _recorded_executable_hash(
            manifest,
            raw_executable.parent,
            raw_executable,
        )
        if recorded_exe_hash:
            build_evidence = " ".join(
                (
                    build_evidence,
                    t(
                        "trust_self_check.manifest.exe_recorded",
                        hash=recorded_exe_hash,
                    ),
                )
            )

    rows = (
        TrustSelfCheckRow(
            key="network",
            claim=network_claim,
            evidence=network_evidence,
            boundary=network_boundary,
        ),
        TrustSelfCheckRow(
            key="drivers",
            claim=driver_claim,
            evidence=driver_evidence,
            boundary=driver_boundary,
        ),
        TrustSelfCheckRow(
            key="background",
            claim=t("trust_self_check.background.claim"),
            evidence=t(
                "trust_self_check.background.evidence",
                run_mode=run_mode,
                pid=os.getpid(),
                executable=executable,
            ),
            boundary=boundary,
        ),
        TrustSelfCheckRow(
            key="local_data",
            claim=t("trust_self_check.local_data.claim"),
            evidence=t(
                "trust_self_check.local_data.evidence",
                data_dir=data_dir,
            ),
            boundary=boundary,
        ),
        TrustSelfCheckRow(
            key="build",
            claim=t("trust_self_check.build.claim"),
            evidence=build_evidence,
            boundary=boundary,
        ),
    )

    return TrustSelfCheckResult(
        generated_at=generated_at,
        version=version,
        build_commit=build_commit,
        build_date=build_date,
        run_mode=run_mode,
        executable_path=executable,
        data_dir=data_dir,
        package_file_count=scan_integrity.python_file_count,
        scan_integrity=scan_integrity,
        network_import_findings=network_findings,
        browser_handoffs=handoffs,
        footprint_findings=footprint_findings,
        rows=rows,
        model_fingerprint=model_fingerprint,
        manifest=manifest,
        payload_verification=payload_verification,
    )


def load_trust_manifest(package_root: str | Path) -> TrustManifest | None:
    """Load a frozen-build record without ever making it a required runtime file."""

    manifest_path = Path(package_root) / TRUST_MANIFEST_FILENAME
    try:
        data = read_guarded_json(
            manifest_path,
            max_bytes=MAX_TRUST_MANIFEST_BYTES,
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _invalid_manifest(manifest_path, f"unreadable: {exc.__class__.__name__}")
    except json.JSONDecodeError:
        return _invalid_manifest(manifest_path, "invalid JSON")
    except RecursionError:
        return _invalid_manifest(manifest_path, "JSON nesting is too deep")
    except ValueError:
        return _invalid_manifest(manifest_path, "JSON exceeds size or nesting limits")
    return _parse_trust_manifest(manifest_path, data)


def verify_payload_against_manifest(
    manifest: TrustManifest,
    dist_root: str | Path,
    *,
    running_executable: str | Path | None = None,
) -> PayloadVerification:
    """Compare payload data files with the build record, never self-hashing the EXE."""

    root = Path(dist_root)
    executable = Path(running_executable or sys.executable)
    executable_relative = _dist_relative_path(executable, root)
    manifest_relative = _dist_relative_path(manifest.manifest_path, root)
    expected = dict(manifest.payload_files)
    mismatched: list[str] = []
    missing: list[str] = []
    matched = 0
    total = 0

    for relative_path, expected_hash in sorted(expected.items()):
        if relative_path == executable_relative:
            continue
        total += 1
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            if not path.is_file():
                missing.append(relative_path)
                continue
            if _sha256_file(path) != expected_hash:
                mismatched.append(relative_path)
                continue
        except OSError:
            mismatched.append(relative_path)
            continue
        matched += 1

    extra_count = 0
    try:
        actual_paths = tuple(path for path in root.rglob("*") if path.is_file())
    except OSError:
        # An unreadable directory means the expected hashes cannot describe the
        # payload completely. Report it through the warning branch, rather than
        # claiming a clean session from a partial walk.
        mismatched.append("<payload directory unreadable>")
        actual_paths = ()
    for path in actual_paths:
        relative_path = _dist_relative_path(path, root)
        if (
            relative_path is None
            or relative_path == manifest_relative
            or relative_path in expected
            or _is_runtime_legitimate_extra(path.name)
        ):
            continue
        extra_count += 1

    return PayloadVerification(
        matched=matched,
        total=total,
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        extra_count=extra_count,
    )


def _parse_trust_manifest(manifest_path: Path, data: object) -> TrustManifest:
    if not isinstance(data, Mapping):
        return _invalid_manifest(manifest_path, "top-level value is not an object")

    required = (
        "schema",
        "version",
        "build_commit",
        "build_commit_short",
        "build_date",
        "generated_at",
        "source_scan",
        "payload_files",
    )
    missing = tuple(key for key in required if key not in data)
    if missing:
        return _invalid_manifest(manifest_path, *(f"missing {key}" for key in missing))
    if data["schema"] != TRUST_MANIFEST_SCHEMA:
        return _invalid_manifest(manifest_path, "unsupported schema")
    string_values = (
        "version",
        "build_commit",
        "build_commit_short",
        "build_date",
        "generated_at",
    )
    if any(not isinstance(data[key], str) or not data[key] for key in string_values):
        return _invalid_manifest(manifest_path, "required metadata is invalid")
    build_commit = str(data["build_commit"])
    build_commit_short = str(data["build_commit_short"])
    if not _is_hex(build_commit, length=40) or not (
        1 <= len(build_commit_short) <= 40
        and _is_hex(build_commit_short)
        and build_commit.startswith(build_commit_short)
    ):
        return _invalid_manifest(manifest_path, "build commit metadata is invalid")

    source_scan = data["source_scan"]
    payload_files = data["payload_files"]
    source_issue = _source_scan_issue(source_scan)
    if source_issue is not None:
        return _invalid_manifest(manifest_path, source_issue)
    payload_issue = _payload_files_issue(payload_files)
    if payload_issue is not None:
        return _invalid_manifest(manifest_path, payload_issue)

    return TrustManifest(
        integrity=ManifestIntegrity(present=True, valid=True),
        manifest_path=manifest_path,
        version=str(data["version"]),
        build_commit=build_commit,
        build_commit_short=build_commit_short,
        build_date=str(data["build_date"]),
        generated_at=str(data["generated_at"]),
        source_scan=dict(source_scan),
        payload_files=dict(payload_files),
    )


def _invalid_manifest(manifest_path: Path, *issues: str) -> TrustManifest:
    return TrustManifest(
        integrity=ManifestIntegrity(
            present=True,
            valid=False,
            reason="invalid",
            issues=tuple(issues),
        ),
        manifest_path=manifest_path,
    )


def _source_scan_issue(source_scan: object) -> str | None:
    if not isinstance(source_scan, Mapping):
        return "source_scan is not an object"
    required = (
        "ruleset",
        "python_file_count",
        "parse_failures",
        "entry_module_scanned",
        "network_import_findings",
        "browser_handoffs",
        "driver_footprint_findings",
        "source_files",
    )
    if any(key not in source_scan for key in required):
        return "source_scan is missing required data"
    if not isinstance(source_scan["ruleset"], Mapping):
        return "source_scan ruleset is invalid"
    for key in ("network_roots", "driver_suffixes", "virtual_device_tokens"):
        values = source_scan["ruleset"].get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return "source_scan ruleset is invalid"
    if type(source_scan["python_file_count"]) is not int or source_scan["python_file_count"] <= 0:
        return "source_scan has no parsed Python files"
    if source_scan["parse_failures"] != []:
        return "source_scan recorded parse failures"
    if source_scan["entry_module_scanned"] is not True:
        return "source_scan did not record the entry module"
    for key in ("network_import_findings", "driver_footprint_findings"):
        if not isinstance(source_scan[key], list) or source_scan[key]:
            return f"source_scan recorded {key}"
    handoffs = source_scan["browser_handoffs"]
    if not isinstance(handoffs, list) or any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("relative_path"), str)
        or type(item.get("line")) is not int
        or not isinstance(item.get("call"), str)
        for item in handoffs
    ):
        return "source_scan browser handoffs are invalid"
    source_files = source_scan["source_files"]
    if (
        not isinstance(source_files, Mapping)
        or not source_files
        or any(
            not isinstance(path, str)
            or not _is_safe_relative_path(path)
            or not isinstance(file_hash, str)
            or not _is_hex(file_hash, length=64)
            for path, file_hash in source_files.items()
        )
    ):
        return "source_scan source files are invalid"
    return None


def _payload_files_issue(payload_files: object) -> str | None:
    if not isinstance(payload_files, Mapping) or not payload_files:
        return "payload_files is invalid"
    if any(
        not isinstance(path, str)
        or not _is_safe_relative_path(path)
        or not isinstance(file_hash, str)
        or not _is_hex(file_hash, length=64)
        for path, file_hash in payload_files.items()
    ):
        return "payload_files is invalid"
    return None


def _is_safe_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _is_hex(value: str, *, length: int | None = None) -> bool:
    return (
        (length is None or len(value) == length)
        and bool(value)
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dist_relative_path(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _is_runtime_legitimate_extra(name: str) -> bool:
    lowered = name.lower()
    return fnmatch(lowered, "unins*.*") or lowered.endswith(".log")


def scan_network_imports(
    package_root: str | Path | None = None,
    *,
    roots: Iterable[str] = NETWORK_IMPORT_ROOTS,
    entry_module_path: str | Path | None = None,
    _scan: _StaticScan | None = None,
) -> tuple[StaticImportFinding, ...]:
    """Statically scan Python imports for networking modules."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    scan = _scan or _collect_static_scan(root, entry_module_path)
    blocked_roots = frozenset(roots)
    findings: list[StaticImportFinding] = []
    for source, tree in scan.parsed_python_files:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_root = alias.name.split(".", 1)[0]
                    if module_root in blocked_roots:
                        findings.append(
                            StaticImportFinding(
                                relative_path=source.relative_path,
                                line=node.lineno,
                                module=alias.name,
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_root = node.module.split(".", 1)[0]
                if module_root in blocked_roots:
                    findings.append(
                        StaticImportFinding(
                            relative_path=source.relative_path,
                            line=node.lineno,
                            module=node.module,
                        )
                    )
        # ``__import__("socket")`` loads socket exactly as the statement does.
        # A dynamic import naming a literal is readable, so it is ruled on here;
        # one that is not readable is not silently allowed either -- it fails
        # the scan through ``_static_analysis_hazards``.
        for parts, line in _dynamic_import_names(tree):
            if parts[0] in blocked_roots:
                findings.append(
                    StaticImportFinding(
                        relative_path=source.relative_path,
                        line=line,
                        module=".".join(parts),
                    )
                )
    return tuple(findings)


def scan_browser_handoffs(
    package_root: str | Path | None = None,
    *,
    entry_module_path: str | Path | None = None,
    _scan: _StaticScan | None = None,
) -> tuple[BrowserHandoff, ...]:
    """Find deliberate browser handoffs without counting them as telemetry."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    scan = _scan or _collect_static_scan(root, entry_module_path)
    handoffs: list[BrowserHandoff] = []
    for source, tree in scan.parsed_python_files:
        webbrowser_names = _webbrowser_aliases(tree)
        if not webbrowser_names:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "open"
                and isinstance(func.value, ast.Name)
                and func.value.id in webbrowser_names
            ):
                handoffs.append(
                    BrowserHandoff(
                        relative_path=source.relative_path,
                        line=node.lineno,
                        call=f"{func.value.id}.open",
                    )
                )
    return tuple(handoffs)


def scan_driver_footprint(
    package_root: str | Path | None = None,
    *,
    entry_module_path: str | Path | None = None,
    _scan: _StaticScan | None = None,
) -> tuple[FootprintFinding, ...]:
    """Scan the shipped package tree for driver or virtual-device artifacts."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    scan = _scan or _collect_static_scan(root, entry_module_path)
    findings: list[FootprintFinding] = []
    for source in scan.files:
        name = source.path.name.lower()
        if source.path.suffix.lower() in DRIVER_ARTIFACT_SUFFIXES:
            findings.append(
                FootprintFinding(
                    relative_path=source.relative_path,
                    reason=t("trust_self_check.drivers.artifact.driver_file"),
                )
            )
        elif any(token in name for token in VIRTUAL_DEVICE_NAME_TOKENS):
            findings.append(
                FootprintFinding(
                    relative_path=source.relative_path,
                    reason=t("trust_self_check.drivers.artifact.virtual_device"),
                )
            )
    return tuple(findings)


def _network_evidence(
    findings: tuple[StaticImportFinding, ...],
    handoffs: tuple[BrowserHandoff, ...],
    *,
    scan_integrity: ScanIntegrity,
) -> str:
    if findings:
        scan = t(
            "trust_self_check.network.evidence.findings",
            count=len(findings),
            findings=_format_import_findings(findings),
        )
    elif _scan_verified(scan_integrity):
        # DELIBERATELY the named-scope count, not the full parsed count. The
        # sentence names "zd_app + main_zd.py", so the number beside it must
        # count exactly those files or the sentence is false. The scan itself is
        # wider -- it also opens the first-party modules those files import from
        # outside the package tree, and a networking import in one of them still
        # produces a finding and still fails the release gate. Reporting the
        # wider number here would describe a scope the sentence does not name.
        # Do not "fix" this back to python_file_count: widening the number
        # requires widening the sentence, which is a three-locale change.
        scan = t(
            (
                "trust_self_check.network.evidence.clean"
                if scan_integrity.entry_module_scanned
                else "trust_self_check.network.evidence.clean_no_entry"
            ),
            modules="/".join(NETWORK_IMPORT_ROOTS),
            py_count=scan_integrity.named_scope_python_file_count,
        )
    else:
        return _unverified_evidence(scan_integrity)
    if handoffs:
        handoff = t(
            "trust_self_check.network.evidence.handoff",
            callsites=_format_handoffs(handoffs),
        )
    else:
        handoff = t("trust_self_check.network.evidence.no_handoff")
    return f"{scan} {handoff}"


def _driver_evidence(
    findings: tuple[FootprintFinding, ...],
    *,
    scan_integrity: ScanIntegrity,
) -> str:
    if findings:
        return t(
            "trust_self_check.drivers.evidence.findings",
            count=len(findings),
            files=_format_footprint_findings(findings),
        )
    if _scan_verified(scan_integrity):
        # Named-scope count for the same reason as in ``_network_evidence``,
        # one notch narrower: this sentence names zd_app ALONE, so it may not
        # report a number swollen by files scanned outside it -- neither the
        # import closure nor the entry module, which sits beside the package
        # rather than inside it. The walk itself is wider than this number in
        # the other direction too (it visits the package's non-Python files,
        # and a driver artifact in any of them still produces a finding), so
        # the sentence under-states its own coverage. That is the safe
        # direction: every file this number claims was scanned really was.
        return t(
            "trust_self_check.drivers.evidence.clean",
            file_count=scan_integrity.named_scope_package_file_count,
        )
    return _unverified_evidence(scan_integrity)


def _manifest_network_evidence(
    manifest: TrustManifest,
    payload: PayloadVerification,
) -> str:
    source_scan = manifest.source_scan
    ruleset = source_scan["ruleset"]
    assert isinstance(ruleset, Mapping)
    roots = ruleset["network_roots"]
    assert isinstance(roots, list)
    evidence = t(
        "trust_self_check.manifest.evidence.network_clean",
        py_count=source_scan["python_file_count"],
        commit=manifest.build_commit_short,
        modules="/".join(str(root) for root in roots),
        matched=payload.matched,
        total=payload.total,
    )
    handoffs = _manifest_browser_handoffs(manifest)
    handoff = (
        t(
            "trust_self_check.network.evidence.handoff",
            callsites=_format_handoffs(handoffs),
        )
        if handoffs
        else t("trust_self_check.network.evidence.no_handoff")
    )
    return _append_payload_extras(f"{evidence} {handoff}", payload)


def _manifest_driver_evidence(
    manifest: TrustManifest,
    payload: PayloadVerification,
) -> str:
    source_scan = manifest.source_scan
    evidence = t(
        "trust_self_check.manifest.evidence.drivers_clean",
        py_count=source_scan["python_file_count"],
        commit=manifest.build_commit_short,
        matched=payload.matched,
        total=payload.total,
    )
    return _append_payload_extras(evidence, payload)


def _payload_warning_evidence(payload: PayloadVerification) -> str:
    evidence = t(
        "trust_self_check.manifest.evidence.payload_warning",
        bad_count=len(payload.mismatched) + len(payload.missing),
        details=_format_payload_problems(payload),
    )
    return _append_payload_extras(evidence, payload)


def _append_payload_extras(evidence: str, payload: PayloadVerification) -> str:
    if not payload.extra_count:
        return evidence
    return " ".join(
        (
            evidence,
            t(
                "trust_self_check.manifest.evidence.extras_note",
                extra_count=payload.extra_count,
            ),
        )
    )


def _format_payload_problems(payload: PayloadVerification) -> str:
    problems = (
        tuple(f"mismatched: {path}" for path in payload.mismatched)
        + tuple(f"missing: {path}" for path in payload.missing)
    )
    return "; ".join(problems[:8])


def _manifest_browser_handoffs(manifest: TrustManifest) -> tuple[BrowserHandoff, ...]:
    raw_handoffs = manifest.source_scan["browser_handoffs"]
    assert isinstance(raw_handoffs, list)
    return tuple(
        BrowserHandoff(
            relative_path=str(item["relative_path"]),
            line=int(item["line"]),
            call=str(item["call"]),
        )
        for item in raw_handoffs
        if isinstance(item, Mapping)
    )


def _recorded_executable_hash(
    manifest: TrustManifest,
    dist_root: Path,
    executable: Path,
) -> str | None:
    executable_relative = _dist_relative_path(executable, dist_root)
    if executable_relative is None:
        return None
    return manifest.payload_files.get(executable_relative)


def _scan_verified(scan_integrity: ScanIntegrity) -> bool:
    return (
        scan_integrity.readable
        and scan_integrity.python_file_count > 0
        and not scan_integrity.parse_failures
    )


def _unverified_evidence(
    scan_integrity: ScanIntegrity,
    *,
    reason: str | None = None,
) -> str:
    return t(
        "trust_self_check.scan.unverified_evidence",
        reason=reason or _scan_integrity_reason(scan_integrity),
        file_count=scan_integrity.python_file_count,
        parse_failures=_format_parse_failures(scan_integrity.parse_failures),
    )


def _scan_integrity_reason(scan_integrity: ScanIntegrity) -> str:
    if not scan_integrity.readable:
        return t("trust_self_check.scan.reason.unreadable")
    if scan_integrity.parse_failures:
        return t("trust_self_check.scan.reason.parse_failures")
    return t("trust_self_check.scan.reason.no_files")


def _run_mode_label(is_frozen: bool, executable: str) -> str:
    if not is_frozen:
        return t("trust_self_check.run_mode.source")
    lowered = executable.replace("/", "\\").lower()
    if "\\program files\\" in lowered or "\\appdata\\local\\programs\\" in lowered:
        return t("trust_self_check.run_mode.installed_frozen")
    return t("trust_self_check.run_mode.portable_frozen")


def _display_path(value: str | Path) -> str:
    text = str(value)
    scrubbed = scrub_paths(text)
    placeholder = _env_placeholder_path(text)
    if placeholder is not None:
        return _collapse_deep_placeholder_path(placeholder)
    return scrubbed


def _env_placeholder_path(text: str) -> str | None:
    for name in ("ZDUL_DATA_DIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        root = os.environ.get(name)
        if not root:
            continue
        replaced = _replace_path_root(text, root, f"%{name}%")
        if replaced is not None:
            return replaced
    return None


def _replace_path_root(text: str, root: str, placeholder: str) -> str | None:
    root_clean = root.rstrip("\\/")
    if not root_clean:
        return None
    normalized_text = text.replace("/", "\\").rstrip("\\/").lower()
    normalized_root = root_clean.replace("/", "\\").lower()
    if normalized_text == normalized_root:
        return placeholder
    prefix = normalized_root + "\\"
    if not normalized_text.startswith(prefix):
        return None
    suffix = text[len(root_clean):].lstrip("\\/")
    if not suffix:
        return placeholder
    return placeholder + "\\" + scrub_paths(suffix)


def _collapse_deep_placeholder_path(path: str) -> str:
    normalized = path.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    if len(parts) <= 3:
        return path
    placeholder = parts[0]
    if not (placeholder.startswith("%") and placeholder.endswith("%")):
        return path
    tail = parts[1:]
    # Collapse only when there is more than one intermediate segment between
    # the placeholder root and the leaf. This preserves common one-leaf paths
    # like %APPDATA%\ZDUltimateLegend while hiding portable/dev worktree names.
    if len(tail) <= 2:
        return path
    return f"{placeholder}\\{_PATH_ELLIPSIS}\\{tail[-1]}"


def _clean_observed_value(value: object) -> str:
    return sanitize_shareable_text(value)


def _format_import_findings(findings: tuple[StaticImportFinding, ...]) -> str:
    return "; ".join(
        f"{finding.relative_path}:{finding.line} imports {finding.module}"
        for finding in findings[:8]
    )


def _format_handoffs(handoffs: tuple[BrowserHandoff, ...]) -> str:
    return "; ".join(
        f"{handoff.call} at {handoff.relative_path}:{handoff.line}"
        for handoff in handoffs[:8]
    )


def _format_footprint_findings(findings: tuple[FootprintFinding, ...]) -> str:
    return "; ".join(
        f"{finding.relative_path} ({finding.reason})" for finding in findings[:8]
    )


def _format_parse_failures(parse_failures: tuple[str, ...]) -> str:
    if not parse_failures:
        return "0"
    return f"{len(parse_failures)} ({'; '.join(parse_failures[:8])})"


def _webbrowser_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "webbrowser":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _parse_python(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def _collect_static_scan(
    root: Path,
    entry_module_path: str | Path | None,
) -> _StaticScan:
    package_files = _package_files(root)
    files = tuple(
        _ScanPath(path=path, relative_path=_rel(path, root))
        for path in package_files.files
    )
    entry_module = _entry_module(root, entry_module_path)
    if entry_module is not None and all(
        source.path != entry_module.path for source in files
    ):
        files += (entry_module,)

    package_paths = frozenset(package_files.files)
    parsed_python_files: list[tuple[_ScanPath, ast.AST]] = []
    parse_failures: list[str] = []
    entry_module_scanned = False
    named_scope_package_file_count = 0
    for source in files:
        if source.path.suffix.lower() != ".py":
            continue
        tree = _parse_python(source.path)
        if tree is None:
            parse_failures.append(source.relative_path)
        else:
            parsed_python_files.append((source, tree))
            # Membership, not "is not the entry module": an entry module that
            # sits INSIDE the package tree is a package file and must still be
            # counted once.
            if source.path in package_paths:
                named_scope_package_file_count += 1
            if entry_module is not None and source.path == entry_module.path:
                entry_module_scanned = True

    # Everything parsed so far is the scope the evidence sentences NAME: the
    # package walk plus the entry module. Captured before the closure widens the
    # parsed set, because the rendered counts must not drift past the scope
    # their sentence names.
    named_scope_python_file_count = len(parsed_python_files)

    # The package tree is not the whole shipped tree: a module the package
    # imports from a sibling directory of the repository is bundled and runs,
    # but a root-only walk never opens it. Follow the imports out of the tree
    # already scanned and pull those files in too, transitively, so the claim
    # covers everything the build actually loads. Reachability rather than a
    # directory list is what keeps this true as imports change; unreachable
    # neighbours stay out, because failing the release on a file that never
    # ships is how a gate gets weakened.
    closure = _imported_source_files(parsed_python_files, root)
    for source, tree in closure.files:
        files += (source,)
        if tree is None:
            parse_failures.append(source.relative_path)
        else:
            parsed_python_files.append((source, tree))

    # A statement-level walk cannot see through a dynamically named import, so
    # any file that performs one is recorded as a scan failure rather than
    # scanned-and-clean. Fail closed: an unreadable import graph is not evidence
    # of no networking. This runs over the closure too, so a dynamically loading
    # sibling is caught as well.
    for source, tree in parsed_python_files:
        parse_failures.extend(
            f"{source.relative_path}:{line} ({reason})"
            for line, reason in _static_analysis_hazards(
                tree, at_repository_root=source.path.parent == root.parent
            )
        )

    return _StaticScan(
        files=files,
        parsed_python_files=tuple(parsed_python_files),
        integrity=ScanIntegrity(
            # A closure that could not be built is not a closure that is empty.
            # Losing the enumeration to an OSError must weaken the scan the same
            # way an unreadable package tree does, or a permission denial reads
            # out as a confident "no networking".
            readable=package_files.readable and closure.complete,
            python_file_count=len(parsed_python_files),
            parse_failures=tuple(parse_failures),
            entry_module_scanned=entry_module_scanned,
            named_scope_python_file_count=named_scope_python_file_count,
            named_scope_package_file_count=named_scope_package_file_count,
        ),
    )


def _imported_source_files(
    parsed_python_files: list[tuple[_ScanPath, ast.AST]],
    root: Path,
) -> _ImportClosure:
    """Resolve first-party imports that land outside ``root``.

    Returns ``(scan_path, tree)`` pairs in deterministic path order, with a
    ``None`` tree for a file that could not be parsed so the caller records it
    as a scan-integrity failure rather than as clean. Only files inside the
    repository root (``root.parent``) resolve; third-party and standard-library
    imports never match a file there and are ignored, which is the correct
    result — this scan is about the first-party source that ships, and the
    ruleset already names the networking roots it refuses.

    ``complete`` is False when the filesystem refused any step of the walk. An
    empty closure and an abandoned closure are indistinguishable from the files
    alone, and only one of them is a clean result, so the caller needs the flag
    to avoid reporting a denial as a verified scan.
    """

    if not parsed_python_files:
        return _ImportClosure(files=(), complete=True)
    repository = root.parent
    try:
        root_resolved = root.resolve()
        repository_resolved = repository.resolve()
    except OSError:
        return _ImportClosure(files=(), complete=False)

    # Every zd_app import is walked, so the same dotted names recur in the
    # thousands; resolving each one once keeps this off the UI thread's budget.
    # A name whose first component has no counterpart at the repository root is
    # third-party or standard library and cannot resolve at all.
    try:
        first_party = {entry.name for entry in repository_resolved.iterdir()}
        first_party |= {entry.stem for entry in repository_resolved.glob("*.py")}
    except OSError:
        return _ImportClosure(files=(), complete=False)
    # Keyed by the candidate KIND as well as the name: a ``from pkg import
    # name`` that was ruled an attribute must not consume the key that a plain
    # ``import pkg.name`` elsewhere still needs.
    attempted: set[tuple[tuple[str, ...], bool]] = set()
    complete = True

    seen: set[Path] = set()
    pending: list[tuple[tuple[str, ...], ast.AST]] = []
    for source, tree in parsed_python_files:
        try:
            resolved = source.path.resolve()
        except OSError:
            # This file's own imports are now unwalkable, so the closure below
            # is missing whatever it would have contributed.
            complete = False
            continue
        seen.add(resolved)
        pending.append((_package_parts(resolved, repository_resolved), tree))

    bindings: dict[Path, frozenset[str]] = {}
    collected: dict[Path, tuple[_ScanPath, ast.AST | None]] = {}
    while pending:
        package, tree = pending.pop()
        for parts, is_attribute_candidate in _import_targets(tree, package):
            key = (parts, is_attribute_candidate)
            if not parts or parts[0] not in first_party or key in attempted:
                continue
            attempted.add(key)
            try:
                if is_attribute_candidate and _package_binds_name(
                    repository_resolved, parts, bindings
                ):
                    # ``from pkg import name`` binds an attribute when
                    # ``pkg/__init__.py`` already defines one, and imports the
                    # submodule only when it does not. Treating every such name
                    # as a submodule pulls unimported files into the scan, which
                    # is how a gate starts failing releases over code that never
                    # loads. Coverage is not lost: if the attribute exists
                    # because the init imported the submodule, the init's own
                    # import statement is walked here too.
                    continue
                candidates = _module_files(repository_resolved, parts)
            except OSError:
                complete = False
                continue
            # ``candidate`` is built by joining onto an already-resolved root,
            # so it needs no second resolve to be a stable dictionary key.
            for candidate in candidates:
                if candidate in seen or candidate.is_relative_to(root_resolved):
                    continue
                seen.add(candidate)
                parsed = _parse_python(candidate)
                collected[candidate] = (
                    _ScanPath(
                        path=candidate,
                        relative_path=_rel(candidate, repository_resolved),
                    ),
                    parsed,
                )
                if parsed is not None:
                    pending.append(
                        (_package_parts(candidate, repository_resolved), parsed)
                    )
    return _ImportClosure(
        files=tuple(collected[key] for key in sorted(collected)),
        complete=complete,
    )


def _package_binds_name(
    repository: Path,
    parts: tuple[str, ...],
    cache: dict[Path, frozenset[str]],
) -> bool:
    """Does ``parts[:-1]``'s package init already bind ``parts[-1]``?

    Only a statically visible module-level binding counts. A package that binds
    lazily (``__getattr__``) or not at all falls through to False, so the
    submodule is still scanned — the uncertain case stays covered.
    """

    init = repository.joinpath(*parts[:-1]) / "__init__.py"
    known = cache.get(init)
    if known is None:
        if not _is_existing_file(init):
            cache[init] = frozenset()
            return False
        tree = _parse_python(init)
        known = frozenset() if tree is None else _module_level_bindings(tree)
        cache[init] = known
    return parts[-1] in known


def _module_level_bindings(tree: ast.AST) -> frozenset[str]:
    """Names an ``__init__.py`` binds at module level, conservatively read."""

    names: set[str] = set()
    body = getattr(tree, "body", [])
    # Top level plus the bodies of top-level ``if``/``try`` blocks: a binding
    # nested deeper is not one this check is willing to claim.
    statements = list(body)
    for statement in body:
        for attribute in ("body", "orelse", "finalbody"):
            statements.extend(getattr(statement, attribute, []) or [])
        for handler in getattr(statement, "handlers", []) or []:
            statements.extend(handler.body)
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            names.add(statement.target.id)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(statement, ast.ImportFrom):
            if statement.level and statement.module is None:
                # ``from . import x`` inside the init binds ``x`` BY importing
                # the submodule. That binding is not evidence that ``x`` is
                # something other than a submodule -- it is the submodule -- so
                # counting it here would hide the very file it loads.
                continue
            for alias in statement.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    return frozenset(names)


def _package_parts(module_path: Path, repository: Path) -> tuple[str, ...]:
    """Dotted package of an already-resolved module path, for relative imports."""

    try:
        relative = module_path.relative_to(repository)
    except ValueError:
        return ()
    # Holds for ``__init__.py`` too: a package init's own package is its
    # directory, which is exactly the path minus the filename.
    return relative.parts[:-1]


def _import_targets(
    tree: ast.AST, package: tuple[str, ...]
) -> tuple[tuple[tuple[str, ...], bool], ...]:
    """Every dotted name an import in ``tree`` could name a module.

    Each entry is ``(parts, is_attribute_candidate)``; the flag marks the
    ``from pkg import name`` case, where ``name`` may be an attribute the
    package already binds rather than a submodule. Imports under a branch the
    interpreter cannot take are skipped -- see ``_live_nodes``.
    """

    dynamic = _dynamic_scan(tree)
    targets: list[tuple[tuple[str, ...], bool]] = []
    for node in dynamic.imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((tuple(alias.name.split(".")), False))
        else:
            if node.level:
                if node.level > len(package):
                    continue
                base = package[: len(package) - (node.level - 1)]
            else:
                base = ()
            prefix = (
                base + tuple(node.module.split(".")) if node.module else base
            )
            if not prefix:
                continue
            targets.append((prefix, False))
            # ``from pkg import mod`` names a submodule, not just an attribute.
            for alias in node.names:
                if alias.name != "*":
                    targets.append((prefix + (alias.name,), True))
    # A dynamic import whose name is a plain literal is as knowable as a
    # statement, so it joins the closure instead of failing the scan.
    for parts, _line in dynamic.resolved:
        targets.append((parts, False))
    return tuple(targets)


def _module_files(repository: Path, parts: tuple[str, ...]) -> tuple[Path, ...]:
    """Files executed by importing ``parts``: each package init, then the module."""

    if not parts or any(not part or part in {".", ".."} for part in parts):
        return ()
    files: list[Path] = []
    for depth in range(1, len(parts)):
        init = repository.joinpath(*parts[:depth]) / "__init__.py"
        if _is_existing_file(init):
            files.append(init)
    leaf = repository.joinpath(*parts)
    module = leaf.with_name(leaf.name + ".py")
    if _is_existing_file(module):
        files.append(module)
    elif _is_existing_file(leaf / "__init__.py"):
        files.append(leaf / "__init__.py")
    return tuple(files)


def _is_existing_file(path: Path) -> bool:
    """``Path.is_file`` with the OSError left in.

    ``Path.is_file()`` answers False for a path it was *refused*, which would
    drop a candidate module out of the scan without a trace. Only "there is
    nothing here" answers False; a denial propagates and weakens the scan.
    """

    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


# Calls that import a module named at runtime. The two resolvers can be read
# statically when their argument is a literal; everything else in this set
# loads source the walk cannot name, so its presence fails the scan closed.
_DYNAMIC_IMPORT_RESOLVERS = frozenset({"__import__", "import_module"})
_DYNAMIC_LOADER_CALLS = frozenset(
    {
        "exec_module",
        "find_module",
        "find_spec",
        "load_module",
        "load_package",
        "load_source",
        "module_from_spec",
        "run_module",
        "run_path",
        "spec_from_file_location",
        "spec_from_loader",
        "ExtensionFileLoader",
        "SourceFileLoader",
        "SourcelessFileLoader",
    }
)
_SYS_PATH_MUTATORS = frozenset({"append", "extend", "insert"})
# Attributes the ``sys.path`` allowance leans on. If a module reassigns any of
# them the canonical shape below proves nothing, so the allowance lapses.
_OWN_DIRECTORY_ATTRS = frozenset(
    {"path", "dirname", "abspath", "realpath", "parent", "resolve"}
)
# The four reasons a file can defeat this walk. They are developer-facing
# detail appended to a scan-failure entry, not localized user copy.
_UNRESOLVED_IMPORT = "unresolved dynamic import"
_DYNAMIC_LOADER = "dynamic module loader"
_SYS_PATH_MUTATION = "unapproved sys.path mutation"
_SYS_PATH_REFERENCE = "unapproved sys.path reference"


@dataclass(frozen=True)
class _NameFacts:
    """What a module's own statements say its names mean.

    Every rule below asks "is this really the name I think it is?" instead of
    matching on spelling, and answers no whenever the module rebinds the name
    anywhere. Both directions of error are real, and they are not symmetric: a
    wrong "no" costs a release-gate cycle, a wrong "yes" publishes a
    no-networking claim the scan never earned. So the rules resolve doubt into
    a hazard.
    """

    # ``__import__``/``import_module`` plus whatever local names were imported
    # as them, and the same for the loader calls.
    resolvers: frozenset[str]
    loaders: frozenset[str]
    # Names bound to the ``sys`` module, and names bound to ``sys.path``.
    sys_aliases: frozenset[str]
    path_aliases: frozenset[str]
    # Names bound to the ``os`` module and to ``pathlib.Path``.
    os_names: frozenset[str]
    pathlib_names: frozenset[str]
    # True when a plain ``import sys`` (no rename) appears.
    plain_sys: bool
    # Names and attributes the module assigns or deletes ANYWHERE, however
    # deeply nested. Kept apart so an ordinary local called ``path`` cannot be
    # mistaken for a rebinding of ``os.path``.
    rebound_names: frozenset[str]
    rebound_attrs: frozenset[str]


@dataclass(frozen=True)
class _DynamicScan:
    # Every live ``import``/``from ... import`` statement. Walk order, not
    # source order; every consumer sorts or de-duplicates its own result.
    imports: tuple[ast.Import | ast.ImportFrom, ...]
    # Dynamic imports whose module name is a literal: ``(dotted parts, line)``.
    resolved: tuple[tuple[tuple[str, ...], int], ...]
    # ``(line, reason, approvable_at_repository_root)``. The last flag marks a
    # ``sys.path`` insert of the module's own directory, which is a hazard
    # everywhere except in the module that sits at the repository root.
    hazards: tuple[tuple[int, str, bool], ...]


def _dynamic_scan(tree: ast.AST) -> _DynamicScan:
    """The live nodes of one file, read for both dynamic-import questions.

    The node list is materialised once and swept a few times rather than
    re-walked, because this runs over every parsed file on the source path and
    the whole scan sits in front of a Diagnostics card.

    Matching a call by the name in front of its parentheses only catches the
    direct spelling. ``loader = importlib.import_module`` followed by
    ``loader("socket")`` imports a networking module while the call reads as
    ``loader``, and so does ``getattr(importlib, "import_module")("socket")``.
    Rather than chase every way a callable can be passed around, the sweep
    below refuses the escape itself: a resolver or loader named anywhere
    OUTSIDE a direct call is a hazard, because from that point on the walk
    cannot say what it imports. ``sys.path`` gets the same treatment through
    its aliases.
    """

    calls: list[ast.Call] = []
    imports: list[ast.Import | ast.ImportFrom] = []
    assigns: list[ast.Assign | ast.AugAssign | ast.AnnAssign] = []
    references: list[ast.Name | ast.Attribute] = []
    definitions: list[ast.AST] = []
    for node in _live_nodes(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            assigns.append(node)
        elif isinstance(node, (ast.Name, ast.Attribute)):
            references.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node)

    facts = _name_facts(imports, assigns, references, definitions)
    resolved: list[tuple[tuple[str, ...], int]] = []
    hazards: list[tuple[int, str, bool]] = []
    # Expression nodes already accounted for by a rule below, so the catch-all
    # escape sweep does not report them a second time.
    consumed: set[int] = set()
    call_funcs = {id(node.func) for node in calls}

    for node in assigns:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            base = target.value if isinstance(target, ast.Subscript) else target
            if _is_sys_path(base, facts):
                consumed.add(id(base))
                hazards.append((node.lineno, _SYS_PATH_MUTATION, False))
        # ``p = sys.path`` is not itself the hazard: it binds an alias this
        # scan tracks, and every use of that alias is classified below. Chained
        # rebinds are not tracked, so they fall through to the escape sweep.
        if (
            isinstance(node, ast.Assign)
            and _is_sys_path_attribute(node.value, facts)
            and all(isinstance(target, ast.Name) for target in node.targets)
        ):
            consumed.add(id(node.value))

    for node in calls:
        name = _call_name(node)
        if name in facts.resolvers:
            parts = _literal_module_name(node)
            if parts is None:
                hazards.append((node.lineno, _UNRESOLVED_IMPORT, False))
            else:
                resolved.append((parts, node.lineno))
        elif name in facts.loaders:
            hazards.append((node.lineno, _DYNAMIC_LOADER, False))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _SYS_PATH_MUTATORS
            and _is_sys_path(node.func.value, facts)
        ):
            consumed.add(id(node.func.value))
            hazards.append(
                (node.lineno, _SYS_PATH_MUTATION, _is_own_directory_insert(node, facts))
            )
        hazards.extend(_getattr_hazards(node, facts))

    for node in references:
        if id(node) in call_funcs:
            continue
        label = node.id if isinstance(node, ast.Name) else node.attr
        if label in facts.resolvers:
            hazards.append((node.lineno, _UNRESOLVED_IMPORT, False))
        elif label in facts.loaders:
            hazards.append((node.lineno, _DYNAMIC_LOADER, False))
        elif id(node) not in consumed and _is_sys_path(node, facts):
            hazards.append((node.lineno, _SYS_PATH_REFERENCE, False))

    return _DynamicScan(
        imports=tuple(imports),
        resolved=tuple(resolved),
        # De-duplicated: one line that trips two sweeps is one scan failure.
        hazards=tuple(sorted(set(hazards))),
    )


def _name_facts(
    imports: list[ast.Import | ast.ImportFrom],
    assigns: list[ast.Assign | ast.AugAssign | ast.AnnAssign],
    references: list[ast.Name | ast.Attribute],
    definitions: list[ast.AST],
) -> _NameFacts:
    """Read a module's own bindings, so the rules match meanings not spellings."""

    resolvers = set(_DYNAMIC_IMPORT_RESOLVERS)
    loaders = set(_DYNAMIC_LOADER_CALLS)
    sys_aliases = {"sys"}
    path_aliases: set[str] = set()
    os_names: set[str] = set()
    pathlib_names: set[str] = set()
    plain_sys = False
    rebound_names: set[str] = set()
    rebound_attrs: set[str] = set()

    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_aliases.add(alias.asname or "sys")
                    plain_sys = plain_sys or alias.asname is None
                elif alias.name in {"os", "os.path"}:
                    # ``import os`` and ``import os.path`` both bind ``os``;
                    # ``import os.path as p`` binds the submodule instead, and
                    # that spelling is simply not one of the shapes matched.
                    if alias.asname is None:
                        os_names.add("os")
                    elif alias.name == "os":
                        os_names.add(alias.asname)
            continue
        for alias in node.names:
            bound = alias.asname or alias.name
            if node.module == "sys" and alias.name == "path":
                path_aliases.add(bound)
            elif node.module == "pathlib" and alias.name == "Path":
                pathlib_names.add(bound)
            elif alias.name in _DYNAMIC_IMPORT_RESOLVERS:
                resolvers.add(bound)
            elif alias.name in _DYNAMIC_LOADER_CALLS:
                loaders.add(bound)

    for node in definitions:
        rebound_names.add(node.name)  # type: ignore[attr-defined]
    for node in references:
        if not isinstance(node.ctx, (ast.Store, ast.Del)):
            continue
        if isinstance(node, ast.Name):
            rebound_names.add(node.id)
        else:
            rebound_attrs.add(node.attr)

    facts = _NameFacts(
        resolvers=frozenset(resolvers),
        loaders=frozenset(loaders),
        sys_aliases=frozenset(sys_aliases),
        path_aliases=frozenset(path_aliases),
        os_names=frozenset(os_names),
        pathlib_names=frozenset(pathlib_names),
        plain_sys=plain_sys,
        rebound_names=frozenset(rebound_names),
        rebound_attrs=frozenset(rebound_attrs),
    )
    # ``p = sys.path`` binds the list itself, so mutations through ``p`` are
    # mutations of the import path. Collected after the rest, because it needs
    # the ``sys`` aliases to recognise the right-hand side.
    for node in assigns:
        if not isinstance(node, ast.Assign):
            continue
        if not _is_sys_path_attribute(node.value, facts):
            continue
        path_aliases.update(
            target.id for target in node.targets if isinstance(target, ast.Name)
        )
    if len(path_aliases) == len(facts.path_aliases):
        return facts
    return replace(facts, path_aliases=frozenset(path_aliases))


def _getattr_hazards(
    node: ast.Call, facts: _NameFacts
) -> tuple[tuple[int, str, bool], ...]:
    """``getattr``/``setattr`` reaching a resolver, a loader or ``sys.path``.

    Attribute access by name is the other way a dynamic import escapes a walk
    that only reads call spellings. Only the dangerous keys are refused, so the
    hundreds of ordinary ``getattr(obj, "field", default)`` calls in this
    package are untouched; a computed key on ``sys`` is refused too, because
    it may well be ``path``.
    """

    if not (
        isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "setattr"}
        and len(node.args) >= 2
    ):
        return ()
    owner, key = node.args[0], node.args[1]
    on_sys = isinstance(owner, ast.Name) and owner.id in facts.sys_aliases
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        if key.value in facts.resolvers:
            return ((node.lineno, _UNRESOLVED_IMPORT, False),)
        if key.value in facts.loaders:
            return ((node.lineno, _DYNAMIC_LOADER, False),)
        if key.value == "path" and on_sys:
            return ((node.lineno, _SYS_PATH_REFERENCE, False),)
        return ()
    if on_sys:
        return ((node.lineno, _SYS_PATH_REFERENCE, False),)
    return ()


def _dynamic_import_names(tree: ast.AST) -> tuple[tuple[tuple[str, ...], int], ...]:
    """Statically resolvable dynamic imports: ``(dotted parts, line)``."""

    return _dynamic_scan(tree).resolved


def _static_analysis_hazards(
    tree: ast.AST, *, at_repository_root: bool
) -> tuple[tuple[int, str], ...]:
    """``(line, reason)`` for every construct that defeats the static walk.

    A file that names its imports at runtime, or that rewrites the import path,
    can load code this scan never opens. The scan cannot prove anything about
    such a file, so it reports failure rather than cleanliness.
    """

    return tuple(
        (line, reason)
        for line, reason, approvable in _dynamic_scan(tree).hazards
        if not (approvable and at_repository_root)
    )


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_module_name(node: ast.Call) -> tuple[str, ...] | None:
    """The dotted name a resolver call imports, when it is a plain literal."""

    if not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    name = first.value
    # A relative name depends on the caller's ``package`` argument, which is
    # one indirection past what this walk reads; treat it as unresolved.
    if not name or name.startswith("."):
        return None
    parts = tuple(name.split("."))
    if not all(part.isidentifier() for part in parts):
        return None
    return parts


def _is_sys_path_attribute(node: ast.AST | None, facts: _NameFacts) -> bool:
    """``sys.path``, under whatever name ``sys`` was imported as."""

    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id in facts.sys_aliases
    )


def _is_sys_path(node: ast.AST | None, facts: _NameFacts) -> bool:
    """``sys.path`` itself, or a local name bound to it."""

    if _is_sys_path_attribute(node, facts):
        return True
    return (
        isinstance(node, ast.Name)
        and node.id in facts.path_aliases
        and isinstance(node.ctx, ast.Load)
    )


def _is_plain_sys_path(node: ast.AST | None, facts: _NameFacts) -> bool:
    """``sys.path`` spelled out, with ``sys`` imported plainly and untouched.

    The allowance below is for one exact statement, so it is granted only to
    the exact spelling. An alias, a rebound ``sys``, or a rebound ``path``
    attribute all mean the expression may not be the import path at all.
    """

    return (
        facts.plain_sys
        and isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and "sys" not in facts.rebound_names
        and "path" not in facts.rebound_attrs
    )


def _is_own_directory_insert(node: ast.Call, facts: _NameFacts) -> bool:
    """Is this call the canonical "put my own directory on the import path"?"""

    attr = node.func.attr  # type: ignore[union-attr]  # checked by the caller
    if attr == "extend":
        return False
    if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
        return False
    if len(node.args) != (2 if attr == "insert" else 1):
        return False
    if not _is_plain_sys_path(node.func.value, facts):  # type: ignore[union-attr]
        return False
    return _is_own_directory(node.args[-1], facts)


def _is_own_directory(node: ast.AST | None, facts: _NameFacts) -> bool:
    """Is this expression *exactly* the module's own directory?

    A module at the repository root adding its own directory to ``sys.path``
    adds the directory the closure already resolves first-party names against,
    so it changes nothing this scan can see. That is only true of the directory
    itself. Anything composed onto it can land elsewhere entirely --
    ``Path(__file__).parent / Path.cwd()`` resolves to the working directory,
    because an absolute right operand wins, and imports from there execute code
    this scan never opened. Asking whether ``__file__`` appears somewhere in
    the expression therefore proves nothing.

    So a closed set of shapes is matched node for node, and every composition
    is refused:

    * ``os.path.dirname(os.path.abspath(__file__))`` -- ``realpath`` too, and
      the bare ``os.path.dirname(__file__)``
    * ``Path(__file__).parent`` and ``Path(__file__).resolve().parent``
    * either of those inside a single ``str(...)``

    Every name the shape leans on must be the one an import bound and must not
    be reassigned in the module, or the shape says nothing about what the
    expression evaluates to.
    """

    if node is None:
        return False
    if facts.rebound_attrs & _OWN_DIRECTORY_ATTRS or "__file__" in facts.rebound_names:
        return False
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and "str" not in facts.rebound_names
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return _os_own_directory(node, facts) or _pathlib_own_directory(node, facts)


def _os_own_directory(node: ast.AST, facts: _NameFacts) -> bool:
    inner = _os_path_call(node, "dirname", facts)
    if inner is None:
        return False
    if _is_file_name(inner, facts):
        return True
    for attr in ("abspath", "realpath"):
        deeper = _os_path_call(inner, attr, facts)
        if deeper is not None and _is_file_name(deeper, facts):
            return True
    return False


def _os_path_call(node: ast.AST, attr: str, facts: _NameFacts) -> ast.expr | None:
    """The sole argument of ``os.path.<attr>(x)``, or None if that is not it."""

    if not (
        isinstance(node, ast.Call)
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "path"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id in facts.os_names
        and node.func.value.value.id not in facts.rebound_names
    ):
        return None
    return node.args[0]


def _pathlib_own_directory(node: ast.AST, facts: _NameFacts) -> bool:
    if not (isinstance(node, ast.Attribute) and node.attr == "parent"):
        return False
    inner: ast.AST = node.value
    if (
        isinstance(inner, ast.Call)
        and not inner.keywords
        and not inner.args
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "resolve"
    ):
        inner = inner.func.value
    return (
        isinstance(inner, ast.Call)
        and not inner.keywords
        and len(inner.args) == 1
        and isinstance(inner.func, ast.Name)
        and inner.func.id in facts.pathlib_names
        and inner.func.id not in facts.rebound_names
        and _is_file_name(inner.args[0], facts)
    )


def _is_file_name(node: ast.AST, facts: _NameFacts) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "__file__"
        and "__file__" not in facts.rebound_names
    )


def _live_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk ``tree``, skipping only branches whose test is a literal constant.

    ``ast.walk`` reports imports under ``if False:`` as though they run. They do
    not: the compiler folds a constant test away, the import is absent from the
    bytecode, and a gate that can fail a release over code the interpreter
    cannot reach is a gate somebody eventually weakens. A literal is also the
    one test that cannot be anything else -- it is spelled out in the node,
    with no name to look up and nothing to rebind.

    ``if TYPE_CHECKING:`` is deliberately NOT pruned, although the idiom means
    "type-only" everywhere it is used here. The test is a name lookup at
    runtime, so it is only False if nothing rebound it, and ``TYPE_CHECKING``
    is an attribute of a module every file may import: ``typing.TYPE_CHECKING
    = True`` anywhere in the process makes the branch live in files that never
    mention the assignment. Proving the negative would take whole-program
    analysis, and this walk reads one file at a time. Pruning it instead let a
    module-level ``TYPE_CHECKING = True`` hide a first-party import from the
    closure entirely -- the file was executed and never opened. For a gate
    behind a no-networking claim, walking a branch that turns out to be dead
    costs a release cycle, while skipping one that turns out to be live ships a
    claim the scan did not earn.
    """

    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If):
            taken = _branch_taken(node.test)
            if taken is True:
                stack.extend(node.body)
                continue
            if taken is False:
                stack.extend(node.orelse)
                continue
        stack.extend(ast.iter_child_nodes(node))


def _branch_taken(test: ast.AST) -> bool | None:
    """True/False when the test is a literal constant, else None."""

    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _branch_taken(test.operand)
        return None if inner is None else not inner
    return None


def _entry_module(
    root: Path,
    entry_module_path: str | Path | None,
) -> _ScanPath | None:
    candidate = (
        Path(entry_module_path)
        if entry_module_path is not None
        else root.parent / "main_zd.py"
    )
    try:
        if not candidate.is_file():
            return None
    except OSError:
        # A module that cannot be inspected must weaken the scan, while an
        # absent module remains an allowed frozen-build condition.
        pass
    return _ScanPath(path=candidate, relative_path=candidate.name)


def _package_files(root: Path) -> _PackageFiles:
    try:
        if not root.is_dir():
            return _PackageFiles(readable=False, files=())
        return _PackageFiles(
            readable=True,
            files=tuple(sorted(path for path in root.rglob("*") if path.is_file())),
        )
    except OSError:
        return _PackageFiles(readable=False, files=())


def _default_package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return scrub_paths(str(path))


def _md(value: object) -> str:
    return escape_markdown(value)


def _model_fingerprint_rows(
    fingerprint: ModelFingerprint,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (t(label_key), _clean_observed_value(value))
        for label_key, value in fingerprint_display_rows(fingerprint)
    )


__all__ = [
    "BOUNDARY_TEXT_KEY",
    "DRIVER_ARTIFACT_SUFFIXES",
    "NETWORK_IMPORT_ROOTS",
    "TRUST_MANIFEST_FILENAME",
    "TRUST_MANIFEST_SCHEMA",
    "VIRTUAL_DEVICE_NAME_TOKENS",
    "BrowserHandoff",
    "FootprintFinding",
    "ManifestIntegrity",
    "PayloadVerification",
    "ScanIntegrity",
    "StaticImportFinding",
    "TrustManifest",
    "TrustSelfCheckResult",
    "TrustSelfCheckRow",
    "build_trust_self_check",
    "load_trust_manifest",
    "scan_browser_handoffs",
    "scan_driver_footprint",
    "scan_network_imports",
    "verify_payload_against_manifest",
]
