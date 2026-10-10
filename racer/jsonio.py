import json
import math
from typing import TypeAlias, cast


JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)

_SIGNED_64_MINIMUM = -(2**63)
_SIGNED_64_MAXIMUM = 2**63 - 1
# Semantic budgets count list/dict containers; scalar values add no depth.
USER_VALUE_MAX_DEPTH = 64
FIXTURE_OUTPUT_WRAPPER_DEPTH = 1  # {"emitted": payload} / {"error": payload}
STAGE_OUTPUT_MAX_DEPTH = USER_VALUE_MAX_DEPTH + FIXTURE_OUTPUT_WRAPPER_DEPTH
STAGE_DOCUMENT_WRAPPER_DEPTH = 3  # document object -> stages list -> stage object
JOB_DOCUMENT_MAX_DEPTH = USER_VALUE_MAX_DEPTH + STAGE_DOCUMENT_WRAPPER_DEPTH
CHECKPOINT_MAX_DEPTH = STAGE_OUTPUT_MAX_DEPTH + STAGE_DOCUMENT_WRAPPER_DEPTH
CLI_RESULT_WRAPPER_DEPTH = 1  # envelope object around the trace result document
CLI_ARTIFACT_MAX_DEPTH = CHECKPOINT_MAX_DEPTH + CLI_RESULT_WRAPPER_DEPTH
# Codec capacity is not permission to bypass the narrower semantic budgets.
JSON_CODEC_MAX_DEPTH = CLI_ARTIFACT_MAX_DEPTH


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_signed_64(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    limit = 2**63 if negative else _SIGNED_64_MAXIMUM
    parsed = 0
    for character in digits:
        digit = ord(character) - ord("0")
        if parsed > (limit - digit) // 10:
            raise ValueError("integer is outside the signed 64-bit range")
        parsed = parsed * 10 + digit
    return -parsed if negative else parsed


def _reject_duplicate_keys(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def strict_json_loads(data: str | bytes | bytearray) -> JsonValue:
    value = cast(
        JsonValue,
        json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
            parse_int=_parse_signed_64,
        ),
    )
    return snapshot_json_value(value, max_depth=JSON_CODEC_MAX_DEPTH)


def require_actual_int(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def validate_json_value(value: object) -> None:
    snapshot_json_value(value)


def snapshot_json_value(
    value: object,
    *,
    max_depth: int = USER_VALUE_MAX_DEPTH,
) -> JsonValue:
    if type(max_depth) is not int or max_depth < 1:
        raise ValueError("max_depth must be a positive integer")
    return _snapshot_json_value(value, set(), 0, max_depth)


def _validate_unicode_scalar_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("strings must not contain surrogate code points")


def _snapshot_json_value(
    value: object,
    active_container_ids: set[int],
    depth: int,
    max_depth: int,
) -> JsonValue:
    if value is None or type(value) is bool:
        return cast(JsonValue, value)
    if type(value) is int:
        if not _SIGNED_64_MINIMUM <= value <= _SIGNED_64_MAXIMUM:
            raise ValueError("integer is outside the signed 64-bit range")
        return value
    if type(value) is str:
        _validate_unicode_scalar_string(value)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite float is not JSON-compatible")
        return value
    if type(value) is list:
        return _snapshot_list(value, active_container_ids, depth, max_depth)
    if type(value) is dict:
        return _snapshot_dict(value, active_container_ids, depth, max_depth)
    raise ValueError(f"{type(value).__name__} is not an exact JSON-compatible value")


def _enter_container(
    value: list[object] | dict[object, object],
    active_container_ids: set[int],
    depth: int,
    max_depth: int,
) -> int:
    next_depth = depth + 1
    if next_depth > max_depth:
        raise ValueError(f"JSON-compatible value exceeds maximum depth {max_depth}")
    container_id = id(value)
    if container_id in active_container_ids:
        raise ValueError("cyclic JSON-compatible value")
    active_container_ids.add(container_id)
    return container_id


def _snapshot_list(
    value: list[object],
    active_container_ids: set[int],
    depth: int,
    max_depth: int,
) -> list[JsonValue]:
    container_id = _enter_container(value, active_container_ids, depth, max_depth)
    try:
        length = len(value)
        snapshot = [
            _snapshot_json_value(item, active_container_ids, depth + 1, max_depth)
            for item in value
        ]
        if len(value) != length:
            raise ValueError("JSON-compatible value changed during snapshot")
        return snapshot
    finally:
        active_container_ids.remove(container_id)


def _snapshot_dict(
    value: dict[object, object],
    active_container_ids: set[int],
    depth: int,
    max_depth: int,
) -> dict[str, JsonValue]:
    container_id = _enter_container(value, active_container_ids, depth, max_depth)
    try:
        length = len(value)
        snapshot: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("dict keys must be strings for JSON-compatible values")
            _validate_unicode_scalar_string(key)
            snapshot[key] = _snapshot_json_value(
                item, active_container_ids, depth + 1, max_depth
            )
        if len(value) != length:
            raise ValueError("JSON-compatible value changed during snapshot")
        return snapshot
    finally:
        active_container_ids.remove(container_id)


def fingerprint_json_bytes(value: object) -> bytes:
    snapshot = snapshot_json_value(value, max_depth=JSON_CODEC_MAX_DEPTH)
    return json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def artifact_json_bytes(value: object) -> bytes:
    return fingerprint_json_bytes(value) + b"\n"
