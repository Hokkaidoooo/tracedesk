"""Install the built wheel, leave the repository, and exercise a real HTTP workflow."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


def request(base, path, *, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    content = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, content, headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return (
            json.loads(raw)
            if "application/json" in response.headers.get("Content-Type", "")
            else raw
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        parser.error("expected one wheel file")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
        check=True,
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TRACEDESK_") and key != "PYTHONPATH"
    }
    environment["PYTHONUTF8"] = "1"
    with TemporaryDirectory(prefix="tracedesk-wheel-") as temporary:
        location = subprocess.run(
            [sys.executable, "-c", "import tracedesk; print(tracedesk.__file__)"],
            cwd=temporary,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if "site-packages" not in location and "dist-packages" not in location:
            raise RuntimeError(f"wheel smoke imported a source checkout: {location}")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (Path(temporary) / "server.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tracedesk.cli",
                    "serve",
                    "--database",
                    str(Path(temporary) / "demo.db"),
                    "--port",
                    str(port),
                ],
                cwd=temporary,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError("wheel server exited: " + log.read())
                    try:
                        request(base, "/health")
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("wheel server did not become healthy") from None
                        time.sleep(0.1)
                html = request(base, "/")
                if b"TraceDesk" not in html:
                    raise RuntimeError("the wheel did not contain the real frontend")
                session = request(base, "/api/sessions", body={"customer_id": "customer-a"})
                turn = request(
                    base,
                    f"/api/sessions/{session['id']}/turns",
                    token=session["token"],
                    body={"message": "申请退款 TD-1001", "request_id": str(uuid4())},
                )
                if turn["status"] != "needs_confirmation" or not turn["citations"]:
                    raise RuntimeError("wheel workflow did not prepare a grounded action")
                action = turn["action"]
                receipt = request(
                    base,
                    f"/api/actions/{action['id']}/confirm",
                    token=session["token"],
                    body={
                        "confirmation_token": action["confirmation_token"],
                        "request_id": str(uuid4()),
                    },
                )
                replay = request(
                    base,
                    f"/api/actions/{action['id']}/confirm",
                    token=session["token"],
                    body={
                        "confirmation_token": action["confirmation_token"],
                        "request_id": str(uuid4()),
                    },
                )
                if receipt["receipt_id"] != replay["receipt_id"] or not replay["replayed"]:
                    raise RuntimeError("wheel confirmation was not idempotent")
                print(
                    json.dumps(
                        {
                            "wheel": wheel.name,
                            "imported_from_installed_package": True,
                            "frontend_http": 200,
                            "workflow": "prepared -> submitted -> replayed",
                            "amount_minor": receipt["amount_minor"],
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
