from typing import ClassVar

from .exit_codes import ExitCode


def normalize_exception(error: Exception) -> tuple[str, str]:
    error_type = type(error).__name__
    if type(error_type) is not str or not error_type:
        error_type = "Exception"
    message = _safe_exception_message(error)
    return _truncate_unicode_scalars(error_type, 128), message


def _safe_exception_message(error: Exception) -> str:
    try:
        value = str(error)
    except Exception:
        value = "<unprintable exception>"
    if type(value) is not str:
        value = "<unprintable exception>"
    return _truncate_unicode_scalars(value, 4096)


def _truncate_unicode_scalars(value: str, maximum: int) -> str:
    sanitized = "".join(
        "�" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in value
    )
    if len(sanitized) <= maximum:
        return sanitized
    return sanitized[: maximum - 3] + "..."


def validate_normalized_error_fields(
    error_type: str,
    message: str,
) -> None:
    _validate_unicode_scalar_field(error_type, "error_type", 128)
    _validate_unicode_scalar_field(message, "error_message", 4096)


def _validate_unicode_scalar_field(
    value: str,
    field_name: str,
    maximum: int,
) -> None:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds maximum length {maximum}")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{field_name} must contain Unicode scalar values")


class RacerError(Exception):
    exit_code: ClassVar[ExitCode]
    error_type: ClassVar[str]


class JobValidationError(RacerError):
    exit_code = ExitCode.INVALID_INPUT
    error_type = "JobValidationError"


class CliArgumentError(JobValidationError):
    error_type = "CliArgumentError"


class UnknownInterruptStageError(JobValidationError):
    error_type = "UnknownInterruptStageError"


class InvalidStateError(RacerError):
    exit_code = ExitCode.INVALID_STATE
    error_type = "InvalidStateError"


class StateTransitionError(InvalidStateError):
    error_type = "StateTransitionError"


class CheckpointNotFoundError(InvalidStateError):
    error_type = "CheckpointNotFoundError"


class CheckpointFingerprintMismatchError(InvalidStateError):
    error_type = "CheckpointFingerprintMismatchError"


class CheckpointFormatError(InvalidStateError):
    error_type = "CheckpointFormatError"


class RevisionConflictError(InvalidStateError):
    error_type = "RevisionConflictError"


class StageFailedError(RacerError):
    exit_code = ExitCode.STAGE_FAILED
    error_type = "StageFailedError"

    def __init__(self, stage_id: str, executor_exit_code: int) -> None:
        super().__init__(
            f"stage '{stage_id}' failed with executor exit code "
            f"{executor_exit_code}"
        )


class CheckpointCommitError(RacerError):
    exit_code = ExitCode.INTERNAL_ERROR
    error_type = "CheckpointCommitError"


class ExecutorContractError(RacerError):
    exit_code = ExitCode.INTERNAL_ERROR
    error_type = "ExecutorContractError"


class SimulatedInterruption(RacerError):
    exit_code = ExitCode.INTERRUPTED
    error_type = "SimulatedInterruption"
