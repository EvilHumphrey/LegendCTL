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
from zd_app.services.path_scrub import scrub_paths
from zd_app.storage.settings_store import _default_user_data_dir


NETWORK_IMPORT_ROOTS = ("socket", "http", "urllib", "requests", "ssl")
DRIVER_ARTIFACT_SUFFIXES = (".sys", ".inf")
VIRTUAL_DEVICE_NAME_TOKENS = ("vigem", "virtualhid", "vhid", "hidguardian")
BOUNDARY_TEXT_KEY = "trust_self_check.boundary.session"


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
    network_import_findings: tuple[StaticImportFinding, ...]
    browser_handoffs: tuple[BrowserHandoff, ...]
    footprint_findings: tuple[FootprintFinding, ...]
    rows: tuple[TrustSelfCheckRow, ...]

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
        return "\n".join(lines).rstrip() + "\n"


def build_trust_self_check(
    *,
    package_root: str | Path | None = None,
    executable_path: str | Path | None = None,
    user_data_dir: str | Path | None = None,
    frozen: bool | None = None,
    now: datetime | None = None,
) -> TrustSelfCheckResult:
    """Assemble the in-session trust evidence without network or device I/O."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    generated_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    executable = _display_path(executable_path or sys.executable)
    data_dir = _display_path(user_data_dir or _default_user_data_dir())
    run_mode = _run_mode_label(is_frozen, str(executable_path or sys.executable))
    network_findings = scan_network_imports(root)
    handoffs = scan_browser_handoffs(root)
    package_file_count = _package_file_count(root)
    footprint_findings = scan_driver_footprint(root)
    version = _clean_observed_value(getattr(app_version, "__version__", ""))
    build_commit = _clean_observed_value(
        getattr(app_version, "__build_commit__", "")
    ) or t("trust_self_check.value.not_embedded")
    build_date = _clean_observed_value(
        getattr(app_version, "__build_date__", "")
    ) or t("trust_self_check.value.not_embedded")
    boundary = t(BOUNDARY_TEXT_KEY)

    rows = (
        TrustSelfCheckRow(
            key="network",
            claim=t("trust_self_check.network.claim"),
            evidence=_network_evidence(network_findings, handoffs),
            boundary=boundary,
        ),
        TrustSelfCheckRow(
            key="drivers",
            claim=t("trust_self_check.drivers.claim"),
            evidence=_driver_evidence(
                footprint_findings,
                package_file_count=package_file_count,
            ),
            boundary=t("trust_self_check.drivers.boundary"),
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
        package_file_count=package_file_count,
        network_import_findings=network_findings,
        browser_handoffs=handoffs,
        footprint_findings=footprint_findings,
        rows=rows,
    )


def scan_network_imports(
    package_root: str | Path | None = None,
    *,
    roots: Iterable[str] = NETWORK_IMPORT_ROOTS,
) -> tuple[StaticImportFinding, ...]:
    """Statically scan Python imports for networking modules."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    blocked_roots = frozenset(roots)
    findings: list[StaticImportFinding] = []
    for path in _python_files(root):
        tree = _parse_python(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_root = alias.name.split(".", 1)[0]
                    if module_root in blocked_roots:
                        findings.append(
                            StaticImportFinding(
                                relative_path=_rel(path, root),
                                line=node.lineno,
                                module=alias.name,
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_root = node.module.split(".", 1)[0]
                if module_root in blocked_roots:
                    findings.append(
                        StaticImportFinding(
                            relative_path=_rel(path, root),
                            line=node.lineno,
                            module=node.module,
                        )
                    )
    return tuple(findings)


def scan_browser_handoffs(
    package_root: str | Path | None = None,
) -> tuple[BrowserHandoff, ...]:
    """Find deliberate browser handoffs without counting them as telemetry."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    handoffs: list[BrowserHandoff] = []
    for path in _python_files(root):
        tree = _parse_python(path)
        if tree is None:
            continue
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
                        relative_path=_rel(path, root),
                        line=node.lineno,
                        call=f"{func.value.id}.open",
                    )
                )
    return tuple(handoffs)


def scan_driver_footprint(
    package_root: str | Path | None = None,
) -> tuple[FootprintFinding, ...]:
    """Scan the shipped package tree for driver or virtual-device artifacts."""

    root = Path(package_root) if package_root is not None else _default_package_root()
    findings: list[FootprintFinding] = []
    for path in _package_files(root):
        name = path.name.lower()
        if path.suffix.lower() in DRIVER_ARTIFACT_SUFFIXES:
            findings.append(
                FootprintFinding(
                    relative_path=_rel(path, root),
                    reason=t("trust_self_check.drivers.artifact.driver_file"),
                )
            )
        elif any(token in name for token in VIRTUAL_DEVICE_NAME_TOKENS):
            findings.append(
                FootprintFinding(
                    relative_path=_rel(path, root),
                    reason=t("trust_self_check.drivers.artifact.virtual_device"),
                )
            )
    return tuple(findings)


def _network_evidence(
    findings: tuple[StaticImportFinding, ...],
    handoffs: tuple[BrowserHandoff, ...],
) -> str:
    if findings:
        scan = t(
            "trust_self_check.network.evidence.findings",
            count=len(findings),
            findings=_format_import_findings(findings),
        )
    else:
        scan = t(
            "trust_self_check.network.evidence.clean",
            modules="/".join(NETWORK_IMPORT_ROOTS),
        )
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
    package_file_count: int,
) -> str:
    if findings:
        return t(
            "trust_self_check.drivers.evidence.findings",
            count=len(findings),
            files=_format_footprint_findings(findings),
        )
    return t(
        "trust_self_check.drivers.evidence.clean",
        file_count=package_file_count,
    )


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
    return placeholder or scrubbed


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


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in _package_files(root) if path.suffix == ".py")


def _package_files(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(path for path in root.rglob("*") if path.is_file()))
    except OSError:
        return ()


def _package_file_count(root: Path) -> int:
    return len(_package_files(root))


def _default_package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return scrub_paths(str(path))


def _md(value: object) -> str:
    return escape_markdown(value)


__all__ = [
    "BOUNDARY_TEXT_KEY",
    "DRIVER_ARTIFACT_SUFFIXES",
    "NETWORK_IMPORT_ROOTS",
    "VIRTUAL_DEVICE_NAME_TOKENS",
    "BrowserHandoff",
    "FootprintFinding",
    "StaticImportFinding",
    "TrustSelfCheckResult",
    "TrustSelfCheckRow",
    "build_trust_self_check",
    "scan_browser_handoffs",
    "scan_driver_footprint",
    "scan_network_imports",
]
