"""Strict JSON at persistence and remote protocol boundaries."""

import json
from math import isfinite
from typing import Any


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def load_json(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def dump_json(value: Any) -> str:
    validate_json(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and isfinite(value):
        return
    if type(value) is list:
        for item in value:
            validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            validate_json(item)
        return
    raise ValueError("Artifacts must contain only finite, strictly JSON-compatible values.")
