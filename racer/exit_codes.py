from enum import IntEnum, unique


@unique
class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    INVALID_STATE = 3
    STAGE_FAILED = 4
    INTERNAL_ERROR = 70
    INTERRUPTED = 130
