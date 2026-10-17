"""The no-network scan must cover shipped source that lives outside ``zd_app``.

``zd_app/services/device_service.py`` imports ``src.xinput``, so ``src/`` holds
production code that the package-tree walk never opened: a networking import
added there would have failed neither the in-app trust self-check nor the
release gate in ``tools/generate_trust_manifest.py``.

The scan set is therefore derived from **import reachability**, not from a
directory list. That is the only formulation that stays true as imports change
and still refuses to fail a release over a module that never ships — the two
unreachable-legacy modules under ``src/`` must stay out of the scan, or the
first false failure gets the gate weakened.

Not every build of this tree has that shape: where the XInput wrapper is
vendored as ``zd_app/services/xinput.py``, the closure never leaves the package.
The synthetic-tree tests carry the gate either way; the real-tree tests at the
bottom check whichever shape the tree in hand actually has.

The load-bearing tests here are the ones that prove the gate FAILS: a gate that
cannot be shown to fail is not a gate.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zd_app.services import trust_self_check


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL_PATH = _REPO_ROOT / "tools" / "generate_trust_manifest.py"
# Keyed on the import itself, not on whether ``src/`` exists, so moving the
# wrapper without changing the import cannot quietly switch the checks below.
_IMPORTS_SIBLING_XINPUT = any(
    (isinstance(node, ast.ImportFrom) and node.module == "src.xinput")
    or (
        isinstance(node, ast.Import)
        and any(alias.name == "src.xinput" for alias in node.names)
    )
    for node in ast.walk(
        ast.parse(
            (_REPO_ROOT / "zd_app" / "services" / "device_service.py").read_text(
                encoding="utf-8"
            )
        )
    )
)
_NOW = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)
_COMMIT = "abcdef0" + "1" * 33
_SHORT_COMMIT = "abcdef0"


def _tool_module():
    spec = importlib.util.spec_from_file_location(
        "generate_trust_manifest_reachability", _TOOL_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mini_repo(
    root: Path,
    *,
    sibling_body: str,
    extra: dict[str, str] | None = None,
    device_body: str = "from src.helper import thing\n\nvalue = thing\n",
):
    """A miniature tree shaped like this one: zd_app imports a ``src`` sibling."""

    repo = root / "repo"
    _write(repo / "zd_app" / "__init__.py", "")
    _write(repo / "zd_app" / "device.py", device_body)
    _write(
        repo / "zd_app" / "version.py",
        '__version__ = "9.9.9"\n__build_commit__ = "abcdef0"\n',
    )
    _write(repo / "main_zd.py", "import zd_app.device\n")
    _write(repo / "src" / "__init__.py", "")
    _write(repo / "src" / "helper.py", sibling_body)
    for relative, body in (extra or {}).items():
        _write(repo / relative, body)

    dist = root / "dist" / "ZDUltimateLegend"
    (dist / "_internal").mkdir(parents=True)
    (dist / "ZD Ultimate Legend.exe").write_bytes(b"fake exe")
    (dist / "_internal" / "payload.dat").write_bytes(b"payload")
    return repo, dist


class ImportedSiblingIsScannedTests(unittest.TestCase):
    """Scanner level — the finding must name the sibling file."""

    def test_a_networking_import_in_an_imported_sibling_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp), sibling_body="import socket\n\nthing = socket\n"
            )

            findings = trust_self_check.scan_network_imports(
                repo / "zd_app",
                entry_module_path=repo / "main_zd.py",
            )

        self.assertEqual(
            [(finding.relative_path, finding.module) for finding in findings],
            [("src/helper.py", "socket")],
        )

    def test_the_walk_is_transitive_and_follows_relative_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="from . import deeper\n\nthing = deeper\n",
                extra={"src/deeper.py": "from urllib import request\n"},
            )

            findings = trust_self_check.scan_network_imports(
                repo / "zd_app",
                entry_module_path=repo / "main_zd.py",
            )

        self.assertEqual(
            [finding.relative_path for finding in findings], ["src/deeper.py"]
        )

    def test_an_unimported_neighbour_is_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="import ctypes\n\nthing = ctypes\n",
                extra={"src/dead.py": "import socket\n"},
            )

            findings = trust_self_check.scan_network_imports(
                repo / "zd_app",
                entry_module_path=repo / "main_zd.py",
            )
            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertEqual(findings, ())
        scanned = {source.relative_path for source, _tree in scan.parsed_python_files}
        self.assertIn("src/helper.py", scanned)
        self.assertNotIn("src/dead.py", scanned)

    def test_an_unparseable_imported_sibling_is_recorded_as_a_scan_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(Path(tmp), sibling_body="def broken(:\n")

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        # Fail toward "the scan is not clean" rather than silently omitting it.
        self.assertIn("src/helper.py", scan.integrity.parse_failures)


class ReleaseGateFailsOnTheNewlyCoveredScopeTests(unittest.TestCase):
    """Release gate level — the manifest build must refuse to produce a record."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _tool_module()

    def _generate(self, repo: Path, dist: Path) -> Path:
        with patch.object(self.tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
            return self.tool.generate_manifest(
                repo_root=repo,
                dist_root=dist,
                generated_at=_NOW,
                scanner_repo_root=_REPO_ROOT,
            )

    def test_the_gate_fails_on_a_networking_import_in_an_imported_sibling(self) -> None:
        for body in (
            "import socket\n\nthing = 1\n",
            "from urllib import request\n\nthing = request\n",
            "import http.client\n\nthing = 1\n",
        ):
            with self.subTest(body=body.splitlines()[0]), tempfile.TemporaryDirectory() as tmp:
                repo, dist = _mini_repo(Path(tmp), sibling_body=body)

                with self.assertRaises(self.tool.ManifestBuildError) as caught:
                    self._generate(repo, dist)

                self.assertIn("network import findings", str(caught.exception))

    def test_the_gate_passes_and_records_the_hash_of_a_clean_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(
                Path(tmp),
                sibling_body="import ctypes\n\nthing = ctypes\n",
                extra={"src/dead.py": "import socket\n"},
            )

            manifest_path = self._generate(repo, dist)
            document = json.loads(manifest_path.read_text(encoding="utf-8"))

        source_files = document["source_scan"]["source_files"]
        self.assertIn("src/helper.py", source_files)
        self.assertIn("src/__init__.py", source_files)
        # The unreachable neighbour is neither scanned nor hashed: no release
        # can be blocked by a module the build never loads.
        self.assertNotIn("src/dead.py", source_files)

    def test_the_gate_fails_when_an_imported_sibling_cannot_be_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(Path(tmp), sibling_body="def broken(:\n")

            with self.assertRaises(self.tool.ManifestBuildError) as caught:
                self._generate(repo, dist)

        self.assertIn("src/helper.py", str(caught.exception))


class DynamicImportFailsClosedTests(unittest.TestCase):
    """A name resolved at runtime is either read, or it fails the scan.

    ``ast.Import``/``ast.ImportFrom`` are not the only ways a module gets
    loaded. ``__import__("socket")`` imports socket exactly as the statement
    does, and ``importlib.import_module("src.net")`` reaches first-party source
    the walk would otherwise never open. A literal argument is as readable as a
    statement and is treated as one; anything the walk cannot read must make the
    scan report failure, because "we could not see" is not "there is nothing".
    """

    def test_a_literal_dynamic_networking_import_is_found(self) -> None:
        for body in (
            '__import__("socket")\n',
            'import importlib\n\nimportlib.import_module("urllib.request")\n',
            'from importlib import import_module as pull\n\npull("http.client")\n',
        ):
            with self.subTest(body=body.splitlines()[-1]), tempfile.TemporaryDirectory() as tmp:
                repo, _dist = _mini_repo(
                    Path(tmp),
                    sibling_body="thing = 1\n",
                    device_body=body,
                )

                findings = trust_self_check.scan_network_imports(
                    repo / "zd_app",
                    entry_module_path=repo / "main_zd.py",
                )

                self.assertEqual(
                    [finding.relative_path for finding in findings], ["device.py"]
                )

    def test_a_literal_dynamic_import_pulls_first_party_source_into_the_scan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body='import importlib\n\nimportlib.import_module("src.net")\n',
                extra={"src/net.py": "import socket\n"},
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )
            findings = trust_self_check.scan_network_imports(
                repo / "zd_app", _scan=scan
            )

        scanned = {source.relative_path for source, _tree in scan.parsed_python_files}
        self.assertIn("src/net.py", scanned)
        self.assertEqual(
            [(finding.relative_path, finding.module) for finding in findings],
            [("src/net.py", "socket")],
        )

    def test_an_unreadable_dynamic_import_is_recorded_as_a_scan_failure(self) -> None:
        for body, expected in (
            ("import importlib\n\nimportlib.import_module(name)\n", "device.py:3"),
            ("__import__(name)\n", "device.py:1"),
            (
                'import importlib\n\nimportlib.import_module(".relative", package="src")\n',
                "device.py:3",
            ),
            (
                "from importlib.util import spec_from_file_location\n\n"
                'spec_from_file_location("m", "m.py")\n',
                "device.py:3",
            ),
            ('import sys\n\nsys.path.append("vendor")\n', "device.py:3"),
        ):
            with self.subTest(body=body.splitlines()[-1]), tempfile.TemporaryDirectory() as tmp:
                repo, _dist = _mini_repo(
                    Path(tmp), sibling_body="thing = 1\n", device_body=body
                )

                scan = trust_self_check._collect_static_scan(
                    repo / "zd_app", repo / "main_zd.py"
                )

                self.assertTrue(
                    any(
                        failure.startswith(expected)
                        for failure in scan.integrity.parse_failures
                    ),
                    scan.integrity.parse_failures,
                )
                self.assertFalse(trust_self_check._scan_verified(scan.integrity))

    def test_the_entry_modules_own_root_path_insert_is_not_a_hazard(self) -> None:
        # ``main_zd.py`` adds its own directory to ``sys.path`` so a source run
        # can import ``zd_app``. That directory is the one the closure already
        # resolves names against, so it changes nothing this scan can see, and
        # failing the real tree closed over it would be a false alarm.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body="value = 1\n",
                extra={
                    "main_zd.py": (
                        "import os\nimport sys\n\n"
                        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
                        "import zd_app.device\n"
                    )
                },
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertEqual(scan.integrity.parse_failures, ())
        self.assertTrue(trust_self_check._scan_verified(scan.integrity))

    def test_a_joined_path_insert_is_a_hazard_even_at_the_repository_root(self) -> None:
        # Anti-vacuity for the test above: the allowance is for the module's own
        # directory, not for any ``__file__``-flavoured expression.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body="value = 1\n",
                extra={
                    "main_zd.py": (
                        "import os\nimport sys\n\n"
                        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'vendor'))\n\n"
                        "import zd_app.device\n"
                    )
                },
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertTrue(
            any(
                failure.startswith("main_zd.py:4")
                for failure in scan.integrity.parse_failures
            ),
            scan.integrity.parse_failures,
        )


class UnreachableCodeIsNotAFalsePositiveTests(unittest.TestCase):
    """The gate must not fail a release over code the interpreter cannot run.

    These are the anti-false-positive counterparts to the "unimported
    neighbour" tests: a neighbour that IS named, but only under a branch that
    never executes, must stay out of the scan for the same reason. A gate that
    can block a release over unreachable code is a gate somebody weakens.
    """

    def _scanned(self, repo: Path) -> set[str]:
        scan = trust_self_check._collect_static_scan(
            repo / "zd_app", repo / "main_zd.py"
        )
        return {source.relative_path for source, _tree in scan.parsed_python_files}

    def test_a_constant_false_branch_does_not_pull_in_a_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body="if False:\n    import src.dead\n",
                extra={"src/dead.py": "import socket\n"},
            )

            scanned = self._scanned(repo)
            findings = trust_self_check.scan_network_imports(
                repo / "zd_app", entry_module_path=repo / "main_zd.py"
            )

        self.assertNotIn("src/dead.py", scanned)
        self.assertEqual(findings, ())

    def test_both_branches_of_a_runtime_test_are_walked(self) -> None:
        # Anti-vacuity for the ``if False:`` narrowing: only a literal decides
        # a branch, so an ordinary runtime test hides nothing on either side.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body=(
                    "import os\n\n"
                    "if os.name == 'nt':\n    import src.win\n"
                    "else:\n    import src.other\n"
                ),
                extra={"src/win.py": "x = 1\n", "src/other.py": "y = 1\n"},
            )

            scanned = self._scanned(repo)

        self.assertIn("src/win.py", scanned)
        self.assertIn("src/other.py", scanned)

    def test_an_attribute_the_package_already_binds_is_not_a_submodule(self) -> None:
        # ``from src import helper`` binds ``src.__init__``'s own ``helper``;
        # Python never imports ``src/helper.py`` here, so neither does the scan.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="import socket\n",
                device_body="from src import helper\n\nvalue = helper\n",
                extra={"src/__init__.py": "helper = None\n"},
            )

            scanned = self._scanned(repo)

        self.assertIn("src/__init__.py", scanned)
        self.assertNotIn("src/helper.py", scanned)

    def test_an_attribute_the_package_imports_is_still_covered(self) -> None:
        # Anti-vacuity for the test above: when the attribute exists *because*
        # the init imported the submodule, the submodule is still scanned --
        # via the init's own import statement.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="import socket\n",
                device_body="from src import helper\n\nvalue = helper\n",
                extra={"src/__init__.py": "from . import helper\n"},
            )

            scanned = self._scanned(repo)
            findings = trust_self_check.scan_network_imports(
                repo / "zd_app", entry_module_path=repo / "main_zd.py"
            )

        self.assertIn("src/helper.py", scanned)
        self.assertEqual(
            [finding.relative_path for finding in findings], ["src/helper.py"]
        )


class IndirectDynamicImportsFailClosedTests(unittest.TestCase):
    """Naming the resolver indirectly must not walk past the gate.

    Matching a call by the name in front of its parentheses only ever caught
    the direct spelling. ``loader = importlib.import_module`` followed by
    ``loader("socket")`` imports a networking module through a call that reads
    as ``loader``; so does ``getattr(importlib, "import_module")("socket")``,
    and the same trick renames ``sys.path``. Each shape below produced no
    hazard, no resolved import and no network finding, which left the release
    manifest on its clean path.

    Every case is proven at the gate, not just at the scanner: the deliverable
    is that ``generate_manifest`` refuses.
    """

    ESCAPES = (
        ("rebound resolver", 'import importlib\nloader = importlib.import_module\nloader("socket")\n'),
        ("getattr resolver", 'import importlib\ngetattr(importlib, "import_module")("socket")\n'),
        ("rebound builtin", 'imp = __import__\nimp("socket")\n'),
        (
            "rebound loader",
            "import importlib.util\n"
            'mk = importlib.util.spec_from_file_location\nmk("m", "m.py")\n',
        ),
        (
            "getattr loader",
            "import importlib.util\n"
            'getattr(importlib.util, "spec_from_file_location")("m", "m.py")\n',
        ),
        ("resolver in a container", 'import importlib\nloaders = {"a": importlib.import_module}\n'),
        ("aliased sys", 'import sys as _s\n_s.path.insert(0, "vendor")\n'),
        ("rebound path list", 'import sys\np = sys.path\np.insert(0, "vendor")\n'),
        ("imported path list", 'from sys import path\npath.append("vendor")\n'),
        ("assigned aliased path", "import sys as _s\n_s.path = []\n"),
        ("getattr path", 'import sys\ngetattr(sys, "path").insert(0, "vendor")\n'),
        ("computed sys attribute", "import sys\ngetattr(sys, name).insert(0, 1)\n"),
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _tool_module()

    def test_every_indirect_form_is_recorded_as_a_scan_failure(self) -> None:
        for label, body in self.ESCAPES:
            with self.subTest(form=label), tempfile.TemporaryDirectory() as tmp:
                repo, _dist = _mini_repo(
                    Path(tmp), sibling_body="thing = 1\n", device_body=body
                )

                scan = trust_self_check._collect_static_scan(
                    repo / "zd_app", repo / "main_zd.py"
                )

                self.assertTrue(
                    any(
                        failure.startswith("device.py:")
                        for failure in scan.integrity.parse_failures
                    ),
                    scan.integrity.parse_failures,
                )
                self.assertFalse(trust_self_check._scan_verified(scan.integrity))

    def test_every_indirect_form_fails_the_release_gate(self) -> None:
        for label, body in self.ESCAPES:
            with self.subTest(form=label), tempfile.TemporaryDirectory() as tmp:
                repo, dist = _mini_repo(
                    Path(tmp), sibling_body="thing = 1\n", device_body=body
                )

                with patch.object(
                    self.tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]
                ):
                    with self.assertRaises(self.tool.ManifestBuildError) as caught:
                        self.tool.generate_manifest(
                            repo_root=repo,
                            dist_root=dist,
                            generated_at=_NOW,
                            scanner_repo_root=_REPO_ROOT,
                        )

                self.assertIn("device.py", str(caught.exception))

    def test_an_ordinary_getattr_is_not_a_hazard(self) -> None:
        # Anti-vacuity: this package makes hundreds of ordinary attribute
        # lookups by name, including on ``sys``. Refusing those would fail the
        # gate on the real tree, which is how an allowance gets widened later.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body=(
                    "import sys\n\n"
                    'frozen = getattr(sys, "frozen", False)\n'
                    'value = getattr(frozen, "bit_length", None)\n'
                ),
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertEqual(scan.integrity.parse_failures, ())
        self.assertTrue(trust_self_check._scan_verified(scan.integrity))


class TheOwnDirectoryAllowanceIsACanonicalShapeTests(unittest.TestCase):
    """The ``sys.path`` allowance must prove "own directory", not resemble it.

    Approving any ``__file__``-flavoured expression with no string constant let
    ``sys.path.insert(0, str(Path(__file__).parent / Path.cwd()))`` through at
    the repository root. The right operand is absolute, so that expression is
    the working directory -- possibly outside the repository entirely -- and
    imports from there execute source this scan never opened. Named constants,
    ``os.sep`` and nested calls widened it the same way.
    """

    def _failures(self, root_body: str) -> tuple[str, ...]:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body="value = 1\n",
                extra={"main_zd.py": root_body + "\nimport zd_app.device\n"},
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )
        return scan.integrity.parse_failures

    def test_a_composed_expression_is_a_hazard_at_the_repository_root(self) -> None:
        for label, body in (
            (
                "absolute right operand",
                "import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).parent / Path.cwd()))\n",
            ),
            (
                "named constant join",
                "import os\nimport sys\n\nVENDOR = 1\n"
                "sys.path.insert(0, os.path.join(os.path.dirname(__file__), VENDOR))\n",
            ),
            (
                "os.sep walk-up",
                "import os\nimport sys\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))"
                " + os.sep + os.pardir)\n",
            ),
            (
                "nested foreign call",
                "import os\nimport sys\n"
                "sys.path.insert(0, plugin_dir(os.path.abspath(__file__)))\n",
            ),
            (
                "splatted arguments",
                "import os\nimport sys\n"
                "sys.path.insert(*(0, os.path.dirname(os.path.abspath(__file__))))\n",
            ),
            (
                "aliased sys",
                "import os\nimport sys as _s\n"
                "_s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            ),
            (
                "rebound os",
                "import os\nimport sys\n\nos = shim\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            ),
        ):
            with self.subTest(form=label):
                self.assertTrue(
                    any(
                        failure.startswith("main_zd.py:")
                        for failure in self._failures(body)
                    ),
                    label,
                )

    def test_the_canonical_shapes_are_allowed_at_the_repository_root(self) -> None:
        for label, body in (
            (
                "os.path.dirname(os.path.abspath(__file__))",
                "import os\nimport sys\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            ),
            (
                "os.path.dirname(__file__)",
                "import os\nimport sys\n"
                "sys.path.insert(0, os.path.dirname(__file__))\n",
            ),
            (
                "str(Path(__file__).resolve().parent)",
                "import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
            ),
            (
                "Path(__file__).parent",
                "import sys\nfrom pathlib import Path\n"
                "sys.path.append(Path(__file__).parent)\n",
            ),
        ):
            with self.subTest(form=label):
                self.assertEqual(self._failures(body), (), label)

    def test_the_canonical_shape_is_still_a_hazard_below_the_root(self) -> None:
        # The allowance exists because the entry module's own directory is the
        # one the closure already resolves names against. A module deeper in
        # the tree adds a DIFFERENT directory, so the same shape is a hazard.
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body=(
                    "import os\nimport sys\n"
                    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                ),
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertTrue(
            any(
                failure.startswith("device.py:3")
                for failure in scan.integrity.parse_failures
            ),
            scan.integrity.parse_failures,
        )

    def test_this_repositorys_own_entry_module_still_passes(self) -> None:
        # The one production call site the allowance has to keep clearing.
        scan = trust_self_check._collect_static_scan(_REPO_ROOT / "zd_app", None)

        self.assertEqual(scan.integrity.parse_failures, ())
        self.assertTrue(scan.integrity.entry_module_scanned)
        self.assertTrue(trust_self_check._scan_verified(scan.integrity))


class ATypeCheckingBranchIsStillWalkedTests(unittest.TestCase):
    """A branch guarded by a name is not a branch this walk can call dead.

    ``TYPE_CHECKING`` is False at runtime only while nothing rebinds it, and it
    is an attribute of a module any file may import -- ``typing.TYPE_CHECKING =
    True`` in one module makes the branch live in another that never mentions
    the assignment. Pruning the branch let a module-level rebinding hide a
    first-party import from the closure: Python executed it, the closure never
    opened the file, and the whole-file network scan therefore never saw what
    was inside it.

    So only a literal ``if False:`` is pruned now. Walking a branch that turns
    out to be dead costs a release cycle; skipping one that turns out to be
    live ships a no-networking claim the scan did not earn.
    """

    def _scanned(self, repo: Path) -> set[str]:
        scan = trust_self_check._collect_static_scan(
            repo / "zd_app", repo / "main_zd.py"
        )
        return {source.relative_path for source, _tree in scan.parsed_python_files}

    REBINDINGS = (
        (
            "module-level rebind",
            "from typing import TYPE_CHECKING\n\nTYPE_CHECKING = True\n"
            "if TYPE_CHECKING:\n    import src.net\n",
        ),
        (
            "module attribute rebind",
            "import typing\n\ntyping.TYPE_CHECKING = True\n"
            "if typing.TYPE_CHECKING:\n    import src.net\n",
        ),
        (
            "rebound elsewhere entirely",
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n    import src.net\n",
        ),
    )

    def test_a_type_checking_import_reaches_the_closure_and_the_finding(self) -> None:
        for label, body in self.REBINDINGS:
            with self.subTest(form=label), tempfile.TemporaryDirectory() as tmp:
                repo, _dist = _mini_repo(
                    Path(tmp),
                    sibling_body="thing = 1\n",
                    device_body=body,
                    extra={"src/net.py": "import socket\n"},
                )

                scanned = self._scanned(repo)
                findings = trust_self_check.scan_network_imports(
                    repo / "zd_app", entry_module_path=repo / "main_zd.py"
                )

                self.assertIn("src/net.py", scanned)
                self.assertEqual(
                    [finding.relative_path for finding in findings], ["src/net.py"]
                )

    def test_a_type_checking_import_fails_the_release_gate(self) -> None:
        tool = _tool_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body=(
                    "from typing import TYPE_CHECKING\n\nTYPE_CHECKING = True\n"
                    "if TYPE_CHECKING:\n    import src.net\n"
                ),
                extra={"src/net.py": "import socket\n"},
            )

            with patch.object(tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
                with self.assertRaises(tool.ManifestBuildError) as caught:
                    tool.generate_manifest(
                        repo_root=repo,
                        dist_root=dist,
                        generated_at=_NOW,
                        scanner_repo_root=_REPO_ROOT,
                    )

        self.assertIn("network import findings", str(caught.exception))

    def test_a_dynamic_import_under_a_type_checking_branch_still_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body=(
                    "import importlib\nfrom typing import TYPE_CHECKING\n\n"
                    "if TYPE_CHECKING:\n    importlib.import_module(name)\n"
                ),
            )

            scan = trust_self_check._collect_static_scan(
                repo / "zd_app", repo / "main_zd.py"
            )

        self.assertTrue(
            any(
                failure.startswith("device.py:5")
                for failure in scan.integrity.parse_failures
            ),
            scan.integrity.parse_failures,
        )

    def test_the_real_trees_type_checking_imports_change_nothing(self) -> None:
        # Anti-vacuity in the other direction: this repository does use the
        # idiom, and walking those branches must not widen the scan or dirty
        # it. Both of its ``if TYPE_CHECKING:`` imports name modules already
        # inside the package tree.
        scan = trust_self_check._collect_static_scan(_REPO_ROOT / "zd_app", None)

        self.assertEqual(scan.integrity.parse_failures, ())
        self.assertEqual(
            trust_self_check.scan_network_imports(_REPO_ROOT / "zd_app", _scan=scan), ()
        )


class ClosureIntegrityFailsClosedTests(unittest.TestCase):
    """A closure that could not be BUILT must not read out as a clean scan.

    An empty closure and an abandoned closure look identical from the file list
    alone. Before this, a filesystem denial during the walk produced an empty
    closure, left ``readable`` true, recorded no failure, and the release gate
    accepted the tree -- a permission error rendered as a confident "no
    networking". Each patch below denies one step of the walk and requires both
    consequences: the user-visible row goes unverified AND the gate refuses.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _tool_module()

    @staticmethod
    def _denying(method: str, denied: Path):
        original = getattr(Path, method)
        # Compared by real path, not by spelling: the release gate resolves its
        # repo root first, so on a runner whose temp dir is an 8.3 short name
        # (``RUNNER~1``) the walk asks about a long-name spelling of ``denied``
        # and an equality test never fires. ``os.path.realpath`` is not the
        # patched ``Path`` method, so this cannot recurse.
        denied_real = os.path.normcase(os.path.realpath(denied))

        def guarded(self, *args, **kwargs):
            if os.path.normcase(os.path.realpath(self)) == denied_real:
                raise PermissionError(13, "denied for the test")
            return original(self, *args, **kwargs)

        return patch.object(Path, method, guarded)

    def _cases(self, repo: Path):
        return (
            ("resolve", repo / "zd_app"),
            ("iterdir", repo.resolve()),
            ("stat", (repo / "src" / "helper.py").resolve()),
        )

    def test_a_denied_walk_step_leaves_the_scan_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(Path(tmp), sibling_body="thing = 1\n")

            for method, denied in self._cases(repo):
                with self.subTest(method=method), self._denying(method, denied):
                    scan = trust_self_check._collect_static_scan(
                        repo / "zd_app", repo / "main_zd.py"
                    )

                    self.assertFalse(scan.integrity.readable)
                    self.assertFalse(trust_self_check._scan_verified(scan.integrity))

    def test_a_denied_walk_step_makes_no_clean_claim_to_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(Path(tmp), sibling_body="thing = 1\n")

            for method, denied in self._cases(repo):
                with self.subTest(method=method), self._denying(method, denied):
                    result = trust_self_check.build_trust_self_check(
                        package_root=repo / "zd_app",
                        entry_module_path=repo / "main_zd.py",
                        now=_NOW,
                    )

                rendered = result.to_text()
                self.assertIn("Network scan did not run", rendered)
                self.assertNotIn("found 0 imports of", rendered)

    def test_a_denied_walk_step_fails_the_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(Path(tmp), sibling_body="thing = 1\n")

            for method, denied in self._cases(repo):
                with self.subTest(method=method), self._denying(method, denied):
                    with patch.object(
                        self.tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]
                    ):
                        with self.assertRaises(self.tool.ManifestBuildError) as caught:
                            self.tool.generate_manifest(
                                repo_root=repo,
                                dist_root=dist,
                                generated_at=_NOW,
                                scanner_repo_root=_REPO_ROOT,
                            )

                    self.assertIn("unreadable", str(caught.exception))


class TheReleaseGateNeedsNoSeparateRulesetTests(unittest.TestCase):
    """One change must close both enforcement points, so prove they are one."""

    def test_the_release_tool_calls_the_shipped_scanners_themselves(self) -> None:
        scanners = _tool_module()._load_real_scanners(_REPO_ROOT)

        self.assertIs(
            scanners["collect_static_scan"], trust_self_check._collect_static_scan
        )
        self.assertIs(
            scanners["scan_network_imports"], trust_self_check.scan_network_imports
        )
        self.assertIs(
            scanners["scan_driver_footprint"], trust_self_check.scan_driver_footprint
        )
        self.assertIs(
            scanners["network_import_roots"], trust_self_check.NETWORK_IMPORT_ROOTS
        )

    def test_the_gate_fails_on_a_dynamically_loaded_networking_module(self) -> None:
        tool = _tool_module()
        for device_body in (
            '__import__("socket")\n',
            'import importlib\n\nimportlib.import_module("src.net")\n',
        ):
            with self.subTest(body=device_body.splitlines()[-1]), tempfile.TemporaryDirectory() as tmp:
                repo, dist = _mini_repo(
                    Path(tmp),
                    sibling_body="thing = 1\n",
                    device_body=device_body,
                    extra={"src/net.py": "import socket\n"},
                )

                with patch.object(tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
                    with self.assertRaises(tool.ManifestBuildError) as caught:
                        tool.generate_manifest(
                            repo_root=repo,
                            dist_root=dist,
                            generated_at=_NOW,
                            scanner_repo_root=_REPO_ROOT,
                        )

                self.assertIn("network import findings", str(caught.exception))

    def test_the_gate_fails_on_an_unreadable_dynamic_import(self) -> None:
        tool = _tool_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(
                Path(tmp),
                sibling_body="thing = 1\n",
                device_body="import importlib\n\nimportlib.import_module(name)\n",
            )

            with patch.object(tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
                with self.assertRaises(tool.ManifestBuildError) as caught:
                    tool.generate_manifest(
                        repo_root=repo,
                        dist_root=dist,
                        generated_at=_NOW,
                        scanner_repo_root=_REPO_ROOT,
                    )

        self.assertIn("device.py:3", str(caught.exception))


class TheReportedCountMatchesTheNamedScopeTests(unittest.TestCase):
    """The evidence sentence names a scope; the number beside it must be that scope.

    Widening the scan widened the count in a sentence that still reads "Static
    scan of zd_app + main_zd.py", which made a literally false statement out of
    a change that only ever made the conclusion more conservative. The count is
    therefore pinned to the named scope while the scan stays wide -- the
    closure files are still opened, still produce findings and still fail the
    gate; they are simply not counted in a sentence that does not name them.
    Widening the sentence instead is a three-locale change and a separate call.
    """

    def test_the_rendered_count_excludes_the_closure_the_sentence_does_not_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _dist = _mini_repo(Path(tmp), sibling_body="thing = 1\n")

            result = trust_self_check.build_trust_self_check(
                package_root=repo / "zd_app",
                entry_module_path=repo / "main_zd.py",
                now=_NOW,
            )

        integrity = result.scan_integrity
        # zd_app/__init__.py, zd_app/device.py, zd_app/version.py, main_zd.py.
        self.assertEqual(integrity.named_scope_python_file_count, 4)
        # The driver sentence names zd_app alone, so main_zd.py -- which sits
        # beside the package, not in it -- is not one of its package files.
        self.assertEqual(integrity.named_scope_package_file_count, 3)
        # ... plus src/__init__.py and src/helper.py, which are scanned.
        self.assertEqual(integrity.python_file_count, 6)
        self.assertIn(
            "Static scan of zd_app + main_zd.py (4 Python files)",
            _network_evidence(result),
        )
        self.assertIn(
            "across 3 package file(s)",
            next(row.evidence for row in result.rows if row.key == "drivers"),
        )

    def test_the_uncounted_closure_still_fails_the_gate(self) -> None:
        # Anti-vacuity: excluding the closure from the COUNT must not exclude it
        # from the scan. If it did, the sentence would be true and worthless.
        tool = _tool_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo, dist = _mini_repo(Path(tmp), sibling_body="import socket\n")

            with patch.object(tool, "_git", side_effect=[_COMMIT, _SHORT_COMMIT]):
                with self.assertRaises(tool.ManifestBuildError) as caught:
                    tool.generate_manifest(
                        repo_root=repo,
                        dist_root=dist,
                        generated_at=_NOW,
                        scanner_repo_root=_REPO_ROOT,
                    )

        self.assertIn("network import findings", str(caught.exception))

    def test_the_real_tree_reports_its_named_scope_not_its_whole_closure(self) -> None:
        result = trust_self_check.build_trust_self_check(now=_NOW)
        integrity = result.scan_integrity

        self.assertGreater(integrity.named_scope_python_file_count, 0)
        self.assertIn(
            f"({integrity.named_scope_python_file_count} Python files)",
            _network_evidence(result),
        )
        if not _IMPORTS_SIBLING_XINPUT:
            # The closure is the named scope, so the two counts agree.
            self.assertEqual(
                integrity.python_file_count,
                integrity.named_scope_python_file_count,
            )
            return
        self.assertGreater(
            integrity.python_file_count, integrity.named_scope_python_file_count
        )
        self.assertNotIn(
            f"({integrity.python_file_count} Python files)",
            _network_evidence(result),
        )


def _network_evidence(result) -> str:
    return next(row.evidence for row in result.rows if row.key == "network")


class RealTreeCoverageTests(unittest.TestCase):
    """The finding in this repository, asserted against this repository."""

    def test_the_shipped_sibling_module_is_in_the_scan_and_the_legacy_ones_are_not(
        self,
    ) -> None:
        scan = trust_self_check._collect_static_scan(_REPO_ROOT / "zd_app", None)
        scanned = {source.relative_path for source, _tree in scan.parsed_python_files}

        if not _IMPORTS_SIBLING_XINPUT:
            # The vendored wrapper is package source, and nothing outside the
            # package is reachable, so nothing outside it is scanned.
            self.assertIn("services/xinput.py", scanned)
            self.assertFalse(
                [path for path in scanned if path.startswith("src/")], scanned
            )
            return
        self.assertIn("src/xinput.py", scanned)
        self.assertIn("src/__init__.py", scanned)
        for unreachable in (
            "src/analyzer.py",
            "src/comparison.py",
            "src/config.py",
            "src/dashboard.py",
            "src/poller.py",
            "src/report.py",
            "src/session.py",
        ):
            with self.subTest(unreachable=unreachable):
                self.assertNotIn(unreachable, scanned)

    def test_the_real_tree_including_the_sibling_still_scans_clean(self) -> None:
        findings = trust_self_check.scan_network_imports(_REPO_ROOT / "zd_app")

        self.assertEqual(findings, ())


if __name__ == "__main__":
    unittest.main()
