"""Discovered wrapper for the isolated real-render picker regression."""

from pathlib import Path
import subprocess
import sys
import unittest


class SafeImportPickerIsolatedTests(unittest.TestCase):
    def test_picker_return_real_render(self):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.isolated_safe_import_picker_return"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=90,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertRegex(output, r"(?s)Ran 1 test in [^\r\n]+\r?\n\r?\nOK\s*\Z")


if __name__ == "__main__":
    unittest.main()
