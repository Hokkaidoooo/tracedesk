import os
import subprocess
import sys


def test_cli_errors_remain_readable_under_windows_legacy_output_encoding():
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("TRACEDESK_")
    }
    environment.update({"PYTHONIOENCODING": "cp1252", "TRACEDESK_PROVIDER": "invalid"})
    result = subprocess.run(
        [sys.executable, "-m", "tracedesk.cli", "probe-model"],
        env=environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "配置或执行错误" in result.stderr.decode("utf-8")
    assert b"UnicodeEncodeError" not in result.stderr
