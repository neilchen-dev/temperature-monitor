from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import ModbusTestServer

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tools" / "modbus_probe.py"


def run_probe(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )


class ModbusProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ModbusTestServer([252, 481, 1], unit_id=1)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_single_read_tcp(self) -> None:
        result = run_probe("--tcp", f"127.0.0.1:{self.server.port}", "--unit", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("25.2", result.stdout)
        self.assertIn("48.1", result.stdout)
        self.assertIn('"source": "modbus"', result.stdout)

    def test_unreachable_target_fails_with_exit_1(self) -> None:
        result = run_probe("--tcp", "127.0.0.1:1", "--timeout", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("失败", result.stdout)

    def test_explicit_scan_reports_units(self) -> None:
        result = run_probe(
            "--tcp", f"127.0.0.1:{self.server.port}",
            "--scan", "--scan-start", "1", "--scan-end", "2",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unit   1 | OK", result.stdout)
        self.assertIn("unit   2 | 失败", result.stdout)
        self.assertIn("响应的 unit id: [1]", result.stdout)

    def test_scan_finds_nothing_exits_1(self) -> None:
        result = run_probe(
            "--tcp", "127.0.0.1:1", "--scan",
            "--scan-start", "1", "--scan-end", "1", "--timeout", "1",
        )
        self.assertEqual(result.returncode, 1)

    def test_transport_arguments_are_exclusive(self) -> None:
        result = run_probe("--tcp", "127.0.0.1:5020", "--rtu", "COM3")
        self.assertNotEqual(result.returncode, 0)

    def test_rtu_missing_port_is_clear_error(self) -> None:
        result = run_probe("--rtu", "COM_DEFINITELY_MISSING_99", "--timeout", "1")
        # 串口不存在：读取失败退出 1，且不崩溃
        self.assertEqual(result.returncode, 1)
        self.assertIn("失败", result.stdout)


if __name__ == "__main__":
    unittest.main()
