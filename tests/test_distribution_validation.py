import importlib.util
import zipfile
from pathlib import Path

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wheel_check = load_script("verify_wheel")
neo4j_check = load_script("verify_neo4j")


def make_wheel(tmp_path, version="9.8.7", *, project="tracedesk"):
    path = tmp_path / f"tracedesk-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{project}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {project}\nVersion: {version}\n",
        )
    return path


def test_wheel_version_comes_from_the_actual_artifact(tmp_path):
    wheel = make_wheel(tmp_path)
    assert wheel_check.wheel_metadata(wheel) == {"name": "tracedesk", "version": "9.8.7"}
    assert wheel_check.find_wheel(None, tmp_path) == wheel.resolve()
    assert wheel_check.find_wheel(wheel, tmp_path) == wheel.resolve()


def test_multiple_wheels_cannot_silently_select_a_stale_version(tmp_path):
    first = make_wheel(tmp_path, "1.0.0")
    make_wheel(tmp_path, "2.0.0")
    with pytest.raises(ValueError, match="多个版本"):
        wheel_check.find_wheel(None, tmp_path)
    assert wheel_check.find_wheel(first, tmp_path) == first.resolve()


def test_wheel_metadata_cannot_be_missing_or_another_project(tmp_path):
    wrong = make_wheel(tmp_path, project="other")
    with pytest.raises(ValueError, match="TraceDesk"):
        wheel_check.wheel_metadata(wrong)
    empty = tmp_path / "empty.whl"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("README", "not a wheel")
    with pytest.raises(ValueError, match="唯一"):
        wheel_check.wheel_metadata(empty)


def test_fresh_workspace_refuses_existing_data_and_source_checkout(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    retained = existing / "retained.txt"
    retained.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        wheel_check.new_workspace(existing)
    assert retained.read_text(encoding="utf-8") == "preserve"
    with pytest.raises(ValueError, match="源码仓库之外"):
        wheel_check.new_workspace(wheel_check.REPOSITORY / "uncreated-wheel-environment")
    created = wheel_check.new_workspace(tmp_path / "fresh")
    assert created.is_dir() and not list(created.iterdir())


def test_inherited_model_and_python_paths_are_not_passed_to_base_install(monkeypatch):
    keys = (
        "TRACEDESK_PROVIDER",
        "TRACEDESK_API_KEY",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PIP_TARGET",
    )
    for key in keys:
        monkeypatch.setenv(key, "test-fixture-value")
    cleaned = wheel_check.isolated_environment()
    assert not any(key in cleaned for key in keys)
    assert cleaned["PYTHONNOUSERSITE"] == "1"


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://127.0.0.1:7687",
        "bolt://localhost:7687",
        "bolt://[::1]:7687",
        "bolt+s://127.0.0.1:7687",
    ],
)
def test_neo4j_live_probe_accepts_only_explicit_direct_loopback_addresses(uri):
    assert neo4j_check.validate_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://database.example.com:7687",
        "bolt://10.0.0.1:7687",
        "bolt://localhost.example.com:7687",
        "bolt://user:password@127.0.0.1:7687",
        "http://127.0.0.1:7474",
        "neo4j://127.0.0.1:7687",
        "bolt://127.0.0.1:7687/database",
        "bolt://127.0.0.1:7687?database=other",
        "bolt://127.0.0.1:0",
    ],
)
def test_neo4j_probe_refuses_remote_routed_or_credential_bearing_addresses(uri):
    with pytest.raises(ValueError):
        neo4j_check.validate_uri(uri)
