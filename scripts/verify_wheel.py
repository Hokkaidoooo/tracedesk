"""Install only the base wheel in a fresh venv and exercise real HTTP outside the checkout."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
import zipfile
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[1]
OPTIONAL_PACKAGES = ("langgraph", "neo4j", "onnxruntime", "tokenizers", "numpy", "graphrag")


def wheel_metadata(wheel: Path) -> dict[str, str]:
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("必须提供实际存在的 wheel 文件。")
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("wheel 必须包含唯一的发行包元数据。")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if metadata.get("Name", "").lower() != "tracedesk" or not metadata.get("Version"):
        raise ValueError("wheel 元数据不是有效的 TraceDesk 发行包。")
    return {"name": metadata["Name"], "version": metadata["Version"]}


def find_wheel(explicit: Path | None, directory: Path) -> Path:
    if explicit is not None:
        wheel = explicit.resolve(strict=True)
        wheel_metadata(wheel)
        return wheel
    wheels = sorted(
        path.resolve()
        for path in directory.glob("*.whl")
        if path.name.lower().startswith("tracedesk-")
    )
    if len(wheels) != 1:
        raise ValueError("发行目录必须恰好包含一个 TraceDesk wheel；有多个版本时请显式指定文件。")
    wheel_metadata(wheels[0])
    return wheels[0]


def isolated_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("TRACEDESK_")
        and key.upper()
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PIP_TARGET",
            "PIP_PREFIX",
            "PIP_USER",
        }
    }
    environment.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    return environment


def new_workspace(path: Path | None) -> Path:
    if path is None:
        if Path(tempfile.gettempdir()).resolve().is_relative_to(REPOSITORY):
            raise ValueError("系统临时目录位于源码仓库内，请显式指定仓库外工作目录。")
        return Path(tempfile.mkdtemp(prefix="tracedesk-wheel-")).resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(REPOSITORY):
        raise ValueError("wheel 验证工作目录必须位于源码仓库之外。")
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def _python(environment: Path) -> list[str]:
    binary = (
        environment / "Scripts" / "python.exe"
        if os.name == "nt"
        else environment / "bin" / "python"
    )
    return [str(binary), "-I", "-X", "utf8"]


def _install(
    wheel: Path,
    workspace: Path,
    environment: dict[str, str],
    *,
    index_url: str | None = None,
) -> dict:
    target = workspace / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target)
    command = [
        *_python(target),
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    if index_url is not None:
        command.extend(["--index-url", index_url])
    command.append(str(wheel))
    with (workspace / "install.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
        subprocess.run(
            [*_python(target), "-m", "pip", "check"],
            cwd=workspace,
            env=environment,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    code = (
        "import importlib.metadata as m,json,site,sys,tracedesk; "
        "installed={d.metadata['Name'].lower().replace('_','-') for d in m.distributions()}; "
        "print(json.dumps({'file':tracedesk.__file__,'prefix':sys.prefix,'version':m.version('tracedesk'),"
        "'user_site':site.ENABLE_USER_SITE,'optional_present':sorted(installed.intersection("
        + repr(set(OPTIONAL_PACKAGES))
        + "))}))"
    )
    process = subprocess.run(
        [*_python(target), "-c", code],
        cwd=workspace,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    installed = json.loads(process.stdout)
    if (
        Path(installed["prefix"]).resolve() != target.resolve()
        or not Path(installed["file"]).resolve().is_relative_to(target.resolve())
        or installed["user_site"] is not False
        or installed["optional_present"]
    ):
        raise RuntimeError("基础 wheel 验证环境混入源码、用户包或可选 AI 依赖。")
    if installed["version"] != wheel_metadata(wheel)["version"]:
        raise RuntimeError("安装后的版本与指定 wheel 元数据不一致。")
    return installed


def request(base: str, path: str, *, body=None, token=None, expected=(200,)) -> tuple[bytes, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    content = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, content, headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        if response.status not in expected:
            raise RuntimeError(
                f"HTTP {path} 返回 {response.status}，预期为 {expected}："
                + raw.decode("utf-8", errors="replace")
            )
        return raw, {key.lower(): value for key, value in response.headers.items()}


def request_json(base: str, path: str, **kwargs) -> dict:
    raw, _ = request(base, path, **kwargs)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("HTTP JSON 响应不是对象。")
    return data


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.paths.add(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.paths.add(values["href"])


def verify_frontend(base: str, wheel: Path) -> dict:
    html, _ = request(base, "/")
    parsed = Assets()
    parsed.feed(html.decode("utf-8"))
    if (
        b"TraceDesk" not in html
        or not any(path.endswith(".js") for path in parsed.paths)
        or not any(path.endswith(".css") for path in parsed.paths)
    ):
        raise RuntimeError("wheel 没有包含真实的前端入口与脚本。")
    with zipfile.ZipFile(wheel) as archive:
        if html != archive.read("tracedesk/web/index.html"):
            raise RuntimeError("HTTP 前端入口与 wheel 内容不同。")
        paths = {
            "/" + name.removeprefix("tracedesk/web/")
            for name in archive.namelist()
            if name.startswith("tracedesk/web/assets/") and not name.endswith("/")
        }
        if not parsed.paths.issubset(paths):
            raise RuntimeError("HTML 引用了 wheel 中不存在的前端资源。")
        for path in sorted(paths):
            content, _ = request(base, path)
            if content != archive.read("tracedesk/web" + path):
                raise RuntimeError("HTTP 资源与 wheel 内容不同：" + path)
    return {"http_status": 200, "asset_count": len(paths), "bytes_match_wheel": True}


def verify_refund(base: str, session: dict) -> dict:
    turn = request_json(
        base,
        f"/api/sessions/{session['id']}/turns",
        token=session["token"],
        body={"message": "申请退款 TD-1001", "request_id": str(uuid4())},
    )
    if turn["status"] != "needs_confirmation" or not turn["citations"]:
        raise RuntimeError("wheel 没有形成带依据的退款确认卡片。")
    action = turn["action"]
    path = f"/api/actions/{action['id']}/confirm"
    receipt = request_json(
        base,
        path,
        token=session["token"],
        body={"confirmation_token": action["confirmation_token"], "request_id": str(uuid4())},
    )
    replay = request_json(
        base,
        path,
        token=session["token"],
        body={"confirmation_token": action["confirmation_token"], "request_id": str(uuid4())},
    )
    if receipt["receipt_id"] != replay["receipt_id"] or not replay["replayed"]:
        raise RuntimeError("wheel 退款确认没有保持幂等。")
    return {"state": "prepared -> submitted -> replayed", "amount_minor": receipt["amount_minor"]}


def verify_knowledge(base: str, session: dict, wheel: Path) -> dict:
    token = session["token"]
    loaded = request_json(
        base,
        "/api/knowledge/demo",
        token=token,
        expected=(201,),
        body={"with_embeddings": False, "with_graph": True},
    )
    snapshot_id = loaded["loaded_snapshot_id"]
    snapshot = request_json(base, "/api/knowledge/snapshots/" + snapshot_id, token=token)
    if (
        len(loaded["documents"]) != 4
        or snapshot["status"] != "published"
        or snapshot["with_embeddings"]
        or snapshot["artifacts"]["graph"]["fact_count"] != 6
        or snapshot["artifacts"]["graph"]["extraction"] != "explicit"
    ):
        raise RuntimeError("基础模式未正确发布四份资料及六条人工来源关系。")
    found = {}
    for method, query in (("lexical", "Y20 USB-A 100W"), ("graph", "X100")):
        result = request_json(
            base,
            "/api/knowledge/search",
            token=token,
            body={"query": query, "method": method, "snapshot_id": snapshot_id, "limit": 5},
        )
        if result["snapshot_id"] != snapshot_id or not result["evidence"]:
            raise RuntimeError("基础知识检索没有返回固定快照的原文证据。")
        found[method] = result["evidence"]
    if not any(len(item["graph_path"]) >= 5 for item in found["graph"]):
        raise RuntimeError("基础图检索没有返回有来源的多跳路径。")
    downloads = set()
    with zipfile.ZipFile(wheel) as archive:
        source_hashes = {
            hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("tracedesk/data/knowledge-demo/") and name.endswith(".md")
        }
    for item in (*found["lexical"], *found["graph"]):
        if item["snapshot_id"] != snapshot_id or item["source_sha256"] not in source_hashes:
            raise RuntimeError("检索来源不属于该 wheel 的固定示例资料。")
        identity = (item["document_id"], item["document_version"])
        if identity in downloads:
            continue
        downloads.add(identity)
        path = f"/api/knowledge/documents/{urllib.parse.quote(identity[0], safe='')}/versions/{identity[1]}/source"
        original, headers = request(base, path, token=token)
        if (
            hashlib.sha256(original).hexdigest() != item["source_sha256"]
            or headers.get("x-source-sha256") != item["source_sha256"]
            or not headers.get("content-disposition", "").startswith("attachment;")
        ):
            raise RuntimeError("来源下载的原文、摘要或响应头不一致。")
    unavailable = request_json(
        base,
        "/api/knowledge/search",
        token=token,
        expected=(409,),
        body={"query": "X100", "method": "semantic", "snapshot_id": snapshot_id},
    )
    if unavailable["error"]["code"] != "RETRIEVAL_UNAVAILABLE":
        raise RuntimeError("基础包未对未配置的语义检索给出明确错误。")
    return {
        "snapshot_id": snapshot_id,
        "documents": 4,
        "manual_facts": 6,
        "methods": {method: len(items) for method, items in found.items()},
        "original_downloads_verified": len(downloads),
        "unconfigured_semantic_rejected": True,
    }


def _wait_for_server(base: str, process: subprocess.Popen, log: Path) -> dict:
    deadline = time.monotonic() + 30
    while True:
        if process.poll() is not None:
            raise RuntimeError("wheel 服务提前退出：" + log.read_text(encoding="utf-8"))
        try:
            health = request_json(base, "/health")
            if health.get("status") != "ok":
                raise RuntimeError("wheel 健康检查没有返回 ok。")
            return health
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise RuntimeError("wheel 服务未在启动时限内响应。") from None
            time.sleep(0.1)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("wheel 服务未在关闭时限内退出，已终止其自有进程。") from exc


def validate(wheel: Path, workspace: Path, *, index_url: str | None = None) -> dict:
    environment = isolated_environment()
    installed = _install(wheel, workspace, environment, index_url=index_url)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = workspace / "server.log"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                *_python(workspace / "venv"),
                "-m",
                "tracedesk.cli",
                "serve",
                "--database",
                str(workspace / "demo.db"),
                "--port",
                str(port),
            ],
            cwd=workspace,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            health = _wait_for_server(base, process, log_path)
            if health["version"] != installed["version"]:
                raise RuntimeError("HTTP 服务版本与 wheel 元数据不一致。")
            frontend = verify_frontend(base, wheel)
            session = request_json(
                base, "/api/sessions", body={"customer_id": "customer-a"}, expected=(201,)
            )
            refund = verify_refund(base, session)
            knowledge = verify_knowledge(base, session, wheel)
        finally:
            _stop(process)
    return {
        "schema": 1,
        "status": "passed",
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "version": installed["version"],
        "package_index": index_url or "pip-default",
        "base_dependencies_only": True,
        "system_site_packages": False,
        "optional_packages_absent": list(OPTIONAL_PACKAGES),
        "imported_from_fresh_venv": True,
        "frontend": frontend,
        "refund": refund,
        "knowledge": knowledge,
        "workspace": str(workspace),
        "workspace_retained": True,
        "server_stopped": True,
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="在仓库外的全新基础虚拟环境验证 wheel。")
    parser.add_argument("wheel", type=Path, nargs="?")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--index-url", help="显式选择依赖索引地址；省略时使用 pip 默认配置，不自动切源。"
    )
    args = parser.parse_args()
    wheel = find_wheel(args.wheel, args.dist_dir)
    workspace = new_workspace(args.work_dir)
    report_path = args.report if args.report is not None else workspace / "wheel-smoke.json"
    try:
        report = validate(wheel, workspace, index_url=args.index_url)
    except Exception as exc:
        # Preserve a failed receipt and the original exception; never install optional extras to mask failure.
        report = {
            "schema": 1,
            "status": "failed",
            "wheel": wheel.name,
            "workspace": str(workspace),
            "workspace_retained": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
