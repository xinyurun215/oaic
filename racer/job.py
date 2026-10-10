import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .errors import JobValidationError
from .jsonio import (
    JOB_DOCUMENT_MAX_DEPTH,
    JsonValue,
    fingerprint_json_bytes,
    snapshot_json_value,
    strict_json_loads,
    validate_json_value,
)


StageAction = Literal["emit", "fail"]
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class RequirementSpec:
    requirement_id: str
    text: str


@dataclass(frozen=True)
class InterfaceSpec:
    interface_id: str
    name: str
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class TestSpec:
    test_id: str
    name: str
    requirement_ids: tuple[str, ...]
    interface_ids: tuple[str, ...]


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    action: StageAction
    payload: JsonValue
    requirement_ids: tuple[str, ...]
    interface_ids: tuple[str, ...]
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class JobSpec:
    schema_version: Literal[1]
    job_id: str
    requirements: tuple[RequirementSpec, ...]
    interfaces: tuple[InterfaceSpec, ...]
    tests: tuple[TestSpec, ...]
    stages: tuple[StageSpec, ...]

    def __post_init__(self) -> None:
        validate_job_spec(self)


_ROOT_FIELDS = {
    "schema_version",
    "job_id",
    "requirements",
    "interfaces",
    "tests",
    "stages",
}
_REQUIREMENT_FIELDS = {"requirement_id", "text"}
_INTERFACE_FIELDS = {"interface_id", "name", "requirement_ids"}
_TEST_FIELDS = {"test_id", "name", "requirement_ids", "interface_ids"}
_STAGE_FIELDS = {
    "stage_id",
    "action",
    "payload",
    "requirement_ids",
    "interface_ids",
    "test_ids",
}


def _object(
    value: JsonValue,
    context: str,
    fields: set[str],
) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise JobValidationError(f"{context} must be an object")
    actual_fields = set(value)
    missing = sorted(fields - actual_fields)
    unknown = sorted(actual_fields - fields)
    if missing:
        raise JobValidationError(
            f"{context} is missing required fields: {', '.join(missing)}"
        )
    if unknown:
        raise JobValidationError(
            f"{context} has unknown fields: {', '.join(unknown)}"
        )
    return value


def _array(value: JsonValue, context: str) -> list[JsonValue]:
    if type(value) is not list:
        raise JobValidationError(f"{context} must be an array")
    return value


def _nonempty_array(value: JsonValue, context: str) -> list[JsonValue]:
    items = _array(value, context)
    if not items:
        raise JobValidationError(f"{context} must not be empty")
    return items


def _string(value: JsonValue, context: str) -> str:
    if type(value) is not str:
        raise JobValidationError(f"{context} must be a string")
    try:
        validate_json_value(value)
    except ValueError as exc:
        raise JobValidationError(f"{context} must be a Unicode scalar string: {exc}") from exc
    return value


def _schema_version(value: JsonValue) -> Literal[1]:
    if type(value) is not int or value != 1:
        raise JobValidationError("schema_version must be the integer 1")
    return 1


def _id(value: JsonValue, context: str) -> str:
    identifier = _string(value, context)
    if _ID_PATTERN.fullmatch(identifier) is None:
        raise JobValidationError(f"{context} is not a valid ID: {identifier!r}")
    return identifier


def _nonblank_string(value: JsonValue, context: str) -> str:
    text = _string(value, context)
    if not text.strip():
        raise JobValidationError(f"{context} must not be blank")
    return text


def _stage_action(value: JsonValue, context: str) -> StageAction:
    action = _string(value, context)
    if action not in ("emit", "fail"):
        raise JobValidationError(f"{context} must be 'emit' or 'fail'")
    return cast(StageAction, action)


def _string_array(value: JsonValue, context: str) -> tuple[str, ...]:
    items = _array(value, context)
    identifiers = tuple(
        _id(item, f"{context}[{index}]")
        for index, item in enumerate(items)
    )
    if len(identifiers) != len(set(identifiers)):
        raise JobValidationError(f"{context} contains duplicate IDs")
    return identifiers


def _nonempty_string_array(value: JsonValue, context: str) -> tuple[str, ...]:
    identifiers = _string_array(value, context)
    if not identifiers:
        raise JobValidationError(f"{context} must not be empty")
    return identifiers


def _tuple(value: object, context: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise JobValidationError(f"{context} must be a tuple")
    return value


def _direct_spec(
    value: object,
    expected_type: type[object],
    context: str,
) -> object:
    if type(value) is not expected_type:
        raise JobValidationError(f"{context} has an invalid item")
    return value


def validate_job_spec(job: JobSpec) -> None:
    _schema_version(job.schema_version)
    _id(job.job_id, "job_id")
    requirements = tuple(
        cast(RequirementSpec, _direct_spec(item, RequirementSpec, "requirements"))
        for item in _tuple(job.requirements, "requirements")
    )
    interfaces = tuple(
        cast(InterfaceSpec, _direct_spec(item, InterfaceSpec, "interfaces"))
        for item in _tuple(job.interfaces, "interfaces")
    )
    tests = tuple(
        cast(TestSpec, _direct_spec(item, TestSpec, "tests"))
        for item in _tuple(job.tests, "tests")
    )
    stages = tuple(
        cast(StageSpec, _direct_spec(item, StageSpec, "stages"))
        for item in _tuple(job.stages, "stages")
    )
    if not requirements:
        raise JobValidationError("requirements must not be empty")
    if not interfaces:
        raise JobValidationError("interfaces must not be empty")
    if not tests:
        raise JobValidationError("tests must not be empty")
    if not stages:
        raise JobValidationError("stages must not be empty")
    for index, item in enumerate(requirements):
        _id(item.requirement_id, f"requirements[{index}].requirement_id")
        _nonblank_string(item.text, f"requirements[{index}].text")
    for index, item in enumerate(interfaces):
        _id(item.interface_id, f"interfaces[{index}].interface_id")
        _nonblank_string(item.name, f"interfaces[{index}].name")
        _direct_reference_ids(item.requirement_ids, f"interfaces[{index}].requirement_ids")
    for index, item in enumerate(tests):
        _id(item.test_id, f"tests[{index}].test_id")
        _nonblank_string(item.name, f"tests[{index}].name")
        _direct_reference_ids(item.requirement_ids, f"tests[{index}].requirement_ids")
        _direct_reference_ids(item.interface_ids, f"tests[{index}].interface_ids")
    for index, item in enumerate(stages):
        _id(item.stage_id, f"stages[{index}].stage_id")
        _stage_action(item.action, f"stages[{index}].action")
        try:
            validate_json_value(item.payload)
        except (ValueError, RecursionError) as exc:
            raise JobValidationError(
                f"stages[{index}].payload is not JSON-compatible: {exc}"
            ) from exc
        _direct_reference_ids(
            item.requirement_ids, f"stages[{index}].requirement_ids", nonempty=True
        )
        _direct_reference_ids(
            item.interface_ids, f"stages[{index}].interface_ids", nonempty=True
        )
        _direct_reference_ids(
            item.test_ids, f"stages[{index}].test_ids", nonempty=True
        )
    _require_unique_ids(requirements, "requirement_id", "requirements")
    _require_unique_ids(interfaces, "interface_id", "interfaces")
    _require_unique_ids(tests, "test_id", "tests")
    _require_unique_ids(stages, "stage_id", "stages")
    requirement_ids = {item.requirement_id for item in requirements}
    interface_ids = {item.interface_id for item in interfaces}
    test_ids = {item.test_id for item in tests}
    for index, item in enumerate(interfaces):
        _require_known_references(item.requirement_ids, requirement_ids, f"interfaces[{index}].requirement_ids")
    for index, item in enumerate(tests):
        _require_known_references(item.requirement_ids, requirement_ids, f"tests[{index}].requirement_ids")
        _require_known_references(item.interface_ids, interface_ids, f"tests[{index}].interface_ids")
    for index, item in enumerate(stages):
        _require_known_references(item.requirement_ids, requirement_ids, f"stages[{index}].requirement_ids")
        _require_known_references(item.interface_ids, interface_ids, f"stages[{index}].interface_ids")
        _require_known_references(item.test_ids, test_ids, f"stages[{index}].test_ids")


def _direct_reference_ids(
    value: object,
    context: str,
    *,
    nonempty: bool = False,
) -> tuple[str, ...]:
    values = _tuple(value, context)
    identifiers = tuple(_id(item, f"{context}[{index}]") for index, item in enumerate(values))
    if nonempty and not identifiers:
        raise JobValidationError(f"{context} must not be empty")
    if len(identifiers) != len(set(identifiers)):
        raise JobValidationError(f"{context} contains duplicate IDs")
    return identifiers


def _require_unique_ids(
    values: tuple[object, ...],
    attribute: str,
    context: str,
) -> None:
    identifiers = [getattr(value, attribute) for value in values]
    if len(identifiers) != len(set(identifiers)):
        raise JobValidationError(f"{context} contains duplicate IDs")


def _require_known_references(
    identifiers: tuple[str, ...],
    known_identifiers: set[str],
    context: str,
) -> None:
    missing = [
        identifier
        for identifier in identifiers
        if identifier not in known_identifiers
    ]
    if missing:
        raise JobValidationError(
            f"{context} references unknown IDs: {', '.join(missing)}"
        )


def _parse_job(value: JsonValue) -> JobSpec:
    root = _object(value, "job", _ROOT_FIELDS)
    requirements = tuple(
        _parse_requirement(item, index)
        for index, item in enumerate(
            _nonempty_array(root["requirements"], "requirements")
        )
    )
    interfaces = tuple(
        _parse_interface(item, index)
        for index, item in enumerate(
            _nonempty_array(root["interfaces"], "interfaces")
        )
    )
    tests = tuple(
        _parse_test(item, index)
        for index, item in enumerate(_nonempty_array(root["tests"], "tests"))
    )
    stages = tuple(
        _parse_stage(item, index)
        for index, item in enumerate(_nonempty_array(root["stages"], "stages"))
    )
    job = JobSpec(
        schema_version=_schema_version(root["schema_version"]),
        job_id=_id(root["job_id"], "job_id"),
        requirements=requirements,
        interfaces=interfaces,
        tests=tests,
        stages=stages,
    )
    _require_unique_ids(requirements, "requirement_id", "requirements")
    _require_unique_ids(interfaces, "interface_id", "interfaces")
    _require_unique_ids(tests, "test_id", "tests")
    _require_unique_ids(stages, "stage_id", "stages")
    requirement_ids = {item.requirement_id for item in requirements}
    interface_ids = {item.interface_id for item in interfaces}
    test_ids = {item.test_id for item in tests}
    for index, item in enumerate(interfaces):
        _require_known_references(
            item.requirement_ids,
            requirement_ids,
            f"interfaces[{index}].requirement_ids",
        )
    for index, item in enumerate(tests):
        _require_known_references(
            item.requirement_ids,
            requirement_ids,
            f"tests[{index}].requirement_ids",
        )
        _require_known_references(
            item.interface_ids,
            interface_ids,
            f"tests[{index}].interface_ids",
        )
    for index, item in enumerate(stages):
        _require_known_references(
            item.requirement_ids,
            requirement_ids,
            f"stages[{index}].requirement_ids",
        )
        _require_known_references(
            item.interface_ids,
            interface_ids,
            f"stages[{index}].interface_ids",
        )
        _require_known_references(
            item.test_ids,
            test_ids,
            f"stages[{index}].test_ids",
        )
    return job


def _parse_requirement(value: JsonValue, index: int) -> RequirementSpec:
    context = f"requirements[{index}]"
    item = _object(value, context, _REQUIREMENT_FIELDS)
    return RequirementSpec(
        requirement_id=_id(item["requirement_id"], f"{context}.requirement_id"),
        text=_nonblank_string(item["text"], f"{context}.text"),
    )


def _parse_interface(value: JsonValue, index: int) -> InterfaceSpec:
    context = f"interfaces[{index}]"
    item = _object(value, context, _INTERFACE_FIELDS)
    return InterfaceSpec(
        interface_id=_id(item["interface_id"], f"{context}.interface_id"),
        name=_nonblank_string(item["name"], f"{context}.name"),
        requirement_ids=_string_array(
            item["requirement_ids"],
            f"{context}.requirement_ids",
        ),
    )


def _parse_test(value: JsonValue, index: int) -> TestSpec:
    context = f"tests[{index}]"
    item = _object(value, context, _TEST_FIELDS)
    return TestSpec(
        test_id=_id(item["test_id"], f"{context}.test_id"),
        name=_nonblank_string(item["name"], f"{context}.name"),
        requirement_ids=_string_array(
            item["requirement_ids"],
            f"{context}.requirement_ids",
        ),
        interface_ids=_string_array(
            item["interface_ids"],
            f"{context}.interface_ids",
        ),
    )


def _parse_stage(value: JsonValue, index: int) -> StageSpec:
    context = f"stages[{index}]"
    item = _object(value, context, _STAGE_FIELDS)
    return StageSpec(
        stage_id=_id(item["stage_id"], f"{context}.stage_id"),
        action=_stage_action(item["action"], f"{context}.action"),
        payload=item["payload"],
        requirement_ids=_nonempty_string_array(
            item["requirement_ids"],
            f"{context}.requirement_ids",
        ),
        interface_ids=_nonempty_string_array(
            item["interface_ids"],
            f"{context}.interface_ids",
        ),
        test_ids=_nonempty_string_array(item["test_ids"], f"{context}.test_ids"),
    )


def load_job(path: str | Path) -> JobSpec:
    try:
        value = strict_json_loads(Path(path).read_bytes())
        job = _parse_job(value)
        fingerprint_json_bytes(_job_to_json_value(job))
        return job
    except JobValidationError:
        raise
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ) as exc:
        raise JobValidationError(f"invalid job input: {exc}") from exc


def snapshot_job_spec(job: JobSpec) -> JobSpec:
    if type(job) is not JobSpec:
        raise JobValidationError("job must be a JobSpec")
    validate_job_spec(job)
    try:
        value = snapshot_json_value(_job_to_json_value(job), max_depth=JOB_DOCUMENT_MAX_DEPTH)
        return _parse_job(value)
    except JobValidationError:
        raise
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise JobValidationError(f"invalid job input: {exc}") from exc


def _job_to_json_value(job: JobSpec) -> dict[str, JsonValue]:
    return {
        "schema_version": job.schema_version,
        "job_id": job.job_id,
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "text": item.text,
            }
            for item in job.requirements
        ],
        "interfaces": [
            {
                "interface_id": item.interface_id,
                "name": item.name,
                "requirement_ids": list(item.requirement_ids),
            }
            for item in job.interfaces
        ],
        "tests": [
            {
                "test_id": item.test_id,
                "name": item.name,
                "requirement_ids": list(item.requirement_ids),
                "interface_ids": list(item.interface_ids),
            }
            for item in job.tests
        ],
        "stages": [
            {
                "stage_id": item.stage_id,
                "action": item.action,
                "payload": item.payload,
                "requirement_ids": list(item.requirement_ids),
                "interface_ids": list(item.interface_ids),
                "test_ids": list(item.test_ids),
            }
            for item in job.stages
        ],
    }


def job_fingerprint(job: JobSpec) -> str:
    canonical_bytes = fingerprint_json_bytes(_job_to_json_value(job))
    return hashlib.sha256(canonical_bytes).hexdigest()
