"""Prove that the installed scanners fail on unsafe synthetic input.

Run with the isolated security environment, not the application environment.
Fixtures are text only. Their unsafe code is never executed.
"""

import json

# Only installed scanners receive fixed synthetic fixtures below, without a shell.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path


class ScannerTests(unittest.TestCase):
    def scan(self, scanner: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        executable = Path(sys.executable).parent / scanner
        return subprocess.run(  # noqa: S603  # nosec B603
            [str(executable), *arguments], capture_output=True, text=True, timeout=60, check=False
        )

    def test_bandit_rejects_eval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "unsafe.py"
            fixture.write_text("def unsafe(value):\n    return eval(value)\n")
            result = self.scan("bandit", ["--format", "json", str(fixture)])
        self.assertEqual(result.returncode, 1, result.stderr)
        findings = json.loads(result.stdout)["results"]
        self.assertIn("B307", {finding["test_id"] for finding in findings})

    def test_zizmor_rejects_workflow_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "unsafe.yml"
            fixture.write_text(
                "name: Unsafe fixture\non: pull_request\npermissions:\n  contents: read\n"
                "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                '      - run: echo "${{ github.event.pull_request.title }}"\n'
            )
            result = self.scan(
                "zizmor", ["--offline", "--min-severity=low", "--format=json", str(fixture)]
            )
        self.assertGreater(result.returncode, 0, result.stderr)
        findings = json.loads(result.stdout)
        self.assertTrue(
            any(item["ident"] == "template-injection" and not item["ignored"] for item in findings)
        )


if __name__ == "__main__":
    unittest.main()
