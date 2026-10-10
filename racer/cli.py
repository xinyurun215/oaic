import argparse
from datetime import datetime
import sys

from .checkpoint import (
    JsonCheckpointStore,
    _event_to_value,
    _snapshot_to_value,
    _stage_to_value,
)
from .clock import SystemClock
from .errors import CliArgumentError, RacerError, SimulatedInterruption, normalize_exception
from .exit_codes import ExitCode
from .job import StageSpec, load_job
from .jsonio import artifact_json_bytes
from .runner import Runner
from .stages import FixtureStageExecutor
from .state import RunResult, StageContext, StageResult
from .traceability import TraceDocument


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


class _TraceExecutor:
    def execute(self, spec: StageSpec, context: StageContext) -> StageResult:
        raise AssertionError("trace must not execute stages")


class _TraceClock:
    def now_utc(self) -> datetime:
        raise AssertionError("trace must not read the clock")


def _command_from_argv(argv: list[str]) -> str | None:
    return argv[0] if argv and argv[0] in {"run", "resume", "reset", "trace"} else None


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "resume", "reset", "trace"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--job", required=True)
        subparser.add_argument("--state-dir", required=True)
        if command in {"run", "resume"}:
            subparser.add_argument("--interrupt-before")
    return parser


def _result_value(result: RunResult) -> dict[str, object]:
    return {
        "job_id": result.job_id,
        "run_id": result.run_id,
        "generation": result.generation,
        "status": result.status.value,
        "durable_status": result.durable_status.value,
        "completed_stage_ids": list(result.completed_stage_ids),
        "failed_stage_id": result.failed_stage_id,
        "exit_code": int(result.exit_code),
        "resumed": result.resumed,
        "checkpoint_revision": result.checkpoint_revision,
    }


def _trace_value(trace: TraceDocument) -> dict[str, object]:
    return {
        "schema_version": trace.schema_version,
        "job_id": trace.job_id,
        "job_fingerprint": trace.job_fingerprint,
        "current_generation": trace.current_generation,
        "current_run_id": trace.current_run_id,
        "current_revision": trace.current_revision,
        "current_run_status": trace.current_run_status.value,
        "stage_states": [_stage_to_value(item) for item in trace.stage_states],
        "traceability": _snapshot_to_value(trace.traceability),
        "events": [_event_to_value(item) for item in trace.events],
    }


def _emit(
    command: str | None,
    exit_code: int,
    error: dict[str, str] | None,
    result: RunResult | TraceDocument | None,
) -> int:
    payload = {
        "command": command,
        "error": error,
        "exit_code": exit_code,
        "ok": exit_code == int(ExitCode.OK),
        "result": (
            None
            if result is None
            else _result_value(result)
            if isinstance(result, RunResult)
            else _trace_value(result)
        ),
    }
    stream = sys.stdout if exit_code == int(ExitCode.OK) else sys.stderr
    data = artifact_json_bytes(payload)
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(data.decode("utf-8"))
    else:
        buffer.write(data)
    return exit_code


def _error_value(error: Exception) -> dict[str, str]:
    error_type, message = normalize_exception(error)
    if isinstance(error, RacerError):
        error_type = error.error_type
    return {"type": error_type, "message": message}


def _persisted_failure(store: JsonCheckpointStore) -> dict[str, str]:
    event = store.load().events[-1]
    return {"type": event.error_type, "message": event.error_message}


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    command = _command_from_argv(values)
    try:
        arguments = _parser().parse_args(values)
    except CliArgumentError as error:
        return _emit(command, int(error.exit_code), _error_value(error), None)
    try:
        job = load_job(arguments.job)
        store = JsonCheckpointStore(arguments.state_dir, job)
        if arguments.command == "trace":
            runner = Runner(job, store, _TraceExecutor(), _TraceClock())
            return _emit(command, int(ExitCode.OK), None, runner.trace())
        runner = Runner(job, store, FixtureStageExecutor(), SystemClock())
        if arguments.command == "run":
            result = runner.run(interrupt_before=arguments.interrupt_before)
        elif arguments.command == "resume":
            result = runner.resume(interrupt_before=arguments.interrupt_before)
        else:
            result = runner.reset()
        exit_code = int(result.exit_code)
        if exit_code == int(ExitCode.INTERRUPTED):
            interruption = SimulatedInterruption(
                f"interrupted before stage '{arguments.interrupt_before}'"
            )
            error = _error_value(interruption)
        elif exit_code != int(ExitCode.OK):
            error = _persisted_failure(store)
        else:
            error = None
        return _emit(command, exit_code, error, result)
    except RacerError as error:
        return _emit(command, int(error.exit_code), _error_value(error), None)
    except KeyboardInterrupt:
        return _emit(
            command,
            int(ExitCode.INTERRUPTED),
            {"type": "KeyboardInterrupt", "message": "interrupted"},
            None,
        )
    except Exception as error:
        return _emit(command, int(ExitCode.INTERNAL_ERROR), _error_value(error), None)
