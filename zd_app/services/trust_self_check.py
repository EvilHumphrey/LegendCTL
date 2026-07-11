"""Trust self-check evidence for the Diagnostics screen."""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
    executable = _display_path(executable_path or sys.executable)
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

    if network_findings or scan_verified:
        network_claim = t("trust_self_check.network.claim")
    else:
        network_claim = t("trust_self_check.scan.unverified_claim.network")
    if footprint_findings or scan_verified:
        driver_claim = t("trust_self_check.drivers.claim")
        driver_boundary = t("trust_self_check.drivers.boundary")
    else:
        driver_claim = t("trust_self_check.scan.unverified_claim.drivers")
        driver_boundary = boundary

    rows = (
        TrustSelfCheckRow(
            key="network",
            claim=network_claim,
            evidence=_network_evidence(
                network_findings,
                handoffs,
                scan_integrity=scan_integrity,
            ),
            boundary=boundary,
        ),
        TrustSelfCheckRow(
            key="drivers",
            claim=driver_claim,
            evidence=_driver_evidence(
                footprint_findings,
                scan_integrity=scan_integrity,
            ),
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
            evidence=t(
                "trust_self_check.build.evidence",
                version=version,
                commit=build_commit,
                date=build_date,
                run_mode=run_mode,
            ),
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
    )


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


def _scan_verified(scan_integrity: ScanIntegrity) -> bool:
    return (
        scan_integrity.readable
        and scan_integrity.python_file_count > 0
        and not scan_integrity.parse_failures
    )


def _unverified_evidence(scan_integrity: ScanIntegrity) -> str:
    return t(
        "trust_self_check.scan.unverified_evidence",
        reason=_scan_integrity_reason(scan_integrity),
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
    "VIRTUAL_DEVICE_NAME_TOKENS",
    "BrowserHandoff",
    "FootprintFinding",
    "ScanIntegrity",
    "StaticImportFinding",
    "TrustSelfCheckResult",
    "TrustSelfCheckRow",
    "build_trust_self_check",
    "scan_browser_handoffs",
    "scan_driver_footprint",
    "scan_network_imports",
]
