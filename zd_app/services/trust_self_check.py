"""Trust self-check evidence for the Diagnostics screen."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from zd_app import version as app_version
from zd_app.i18n import t
from zd_app.services.markdown_safety import escape_markdown
from zd_app.services.model_fingerprint import ModelFingerprint, fingerprint_display_rows
from zd_app.services.path_scrub import scrub_paths
from zd_app.storage.settings_store import _default_user_data_dir


NETWORK_IMPORT_ROOTS = ("socket", "http", "urllib", "requests", "ssl")
DRIVER_ARTIFACT_SUFFIXES = (".sys", ".inf")
VIRTUAL_DEVICE_NAME_TOKENS = ("vigem", "virtualhid", "vhid", "hidguardian")
BOUNDARY_TEXT_KEY = "trust_self_check.boundary.session"
TRUST_MANIFEST_FILENAME = "trust_manifest.json"
TRUST_MANIFEST_SCHEMA = 1
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
        raw = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _invalid_manifest(manifest_path, f"unreadable: {exc.__class__.__name__}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _invalid_manifest(manifest_path, "invalid JSON")
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
        scan = t(
            (
                "trust_self_check.network.evidence.clean"
                if scan_integrity.entry_module_scanned
                else "trust_self_check.network.evidence.clean_no_entry"
            ),
            modules="/".join(NETWORK_IMPORT_ROOTS),
            py_count=scan_integrity.python_file_count,
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
        return t(
            "trust_self_check.drivers.evidence.clean",
            file_count=scan_integrity.python_file_count,
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
    return scrub_paths("" if value is None else str(value))


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

    parsed_python_files: list[tuple[_ScanPath, ast.AST]] = []
    parse_failures: list[str] = []
    entry_module_scanned = False
    for source in files:
        if source.path.suffix.lower() != ".py":
            continue
        tree = _parse_python(source.path)
        if tree is None:
            parse_failures.append(source.relative_path)
        else:
            parsed_python_files.append((source, tree))
            if entry_module is not None and source.path == entry_module.path:
                entry_module_scanned = True

    return _StaticScan(
        files=files,
        parsed_python_files=tuple(parsed_python_files),
        integrity=ScanIntegrity(
            readable=package_files.readable,
            python_file_count=len(parsed_python_files),
            parse_failures=tuple(parse_failures),
            entry_module_scanned=entry_module_scanned,
        ),
    )


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
