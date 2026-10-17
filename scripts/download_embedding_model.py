"""Download a pinned, checksum-verified Chinese BGE ONNX model without changing app config."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

MODEL = "Xenova/bge-small-zh-v1.5"
REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
FILES = {
    "onnx/model_quantized.onnx": (
        24_010_842,
        "sha256",
        "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc",
    ),
    "tokenizer.json": (439_125, "git", "cdb3043fc938fc918c06e66cf704c2ba58f88747"),
    "config.json": (716, "git", "d590a6643f5dcdd0cfb0477e3c291488c16fc2d7"),
    "tokenizer_config.json": (367, "git", "3a59388f0fd1bd22dec2ce7902c1be8e1fb84107"),
    "README.md": (1057, "git", "e26ec7fcc01a7aff66171d44acff0a80fae865e1"),
}


def verify(path: Path, expected: tuple[int, str, str]) -> dict:
    size, algorithm, digest = expected
    content = path.read_bytes()
    actual = (
        hashlib.sha256(content).hexdigest()
        if algorithm == "sha256"
        else hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    )
    if len(content) != size or actual != digest:
        raise ValueError(f"Model file does not match the pinned upstream object: {path}")
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def download(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, expected in FILES.items():
        target = directory / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".download")
            # An interrupted transfer is explicit; never overwrite an unknown partial or final file.
            with urlopen(
                f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}", timeout=120
            ) as response:
                with partial.open("xb") as output:
                    received = 0
                    while block := response.read(1024 * 1024):
                        received += len(block)
                        if received > expected[0]:
                            raise ValueError(f"Download exceeded the pinned size: {name}")
                        output.write(block)
            verify(partial, expected)
            partial.rename(target)
        results[name] = verify(target, expected)
    report = {"model": MODEL, "revision": REVISION, "dimensions": 512, "files": results}
    manifest = directory / "verified-model.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if manifest.exists() and manifest.read_text(encoding="utf-8") != serialized:
        raise ValueError("Existing model manifest differs from the verified result.")
    if not manifest.exists():
        with manifest.open("x", encoding="utf-8") as output:
            output.write(serialized)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(download(args.directory), ensure_ascii=False))


if __name__ == "__main__":
    main()
