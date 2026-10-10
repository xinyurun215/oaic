import importlib
import unittest
from dataclasses import FrozenInstanceError, fields

from racer.errors import InvalidStateError, StateTransitionError
from racer.exit_codes import ExitCode
from racer.jsonio import JsonValue


state_module = importlib.import_module("racer.state")


class StateTransitionTests(unittest.TestCase):
    def test_all_legal_stage_transitions(self) -> None:
        cases = (
            (state_module.StageStatus.PENDING, state_module.StageStatus.RUNNING, 0, 1),
            (state_module.StageStatus.RUNNING, state_module.StageStatus.SUCCEEDED, 1, 1),
            (state_module.StageStatus.RUNNING, state_module.StageStatus.FAILED, 1, 1),
            (state_module.StageStatus.RUNNING, state_module.StageStatus.INTERRUPTED, 1, 1),
            (state_module.StageStatus.INTERRUPTED, state_module.StageStatus.RUNNING, 1, 2),
        )

        for current, target, attempts, expected_attempts in cases:
            with self.subTest(current=current, target=target):
                original = state_module.StageState(
                    stage_id="build",
                    status=current,
                    attempts=attempts,
                    exit_code=None,
                    output=None,
                )

                result = state_module.transition_stage_state(original, target)

                self.assertEqual(result.status, target)
                self.assertEqual(result.attempts, expected_attempts)

    def test_all_legal_run_transitions(self) -> None:
        self.assertTrue(
            hasattr(state_module, "RunStatus"), "RunStatus is not implemented"
        )
        self.assertTrue(
            hasattr(state_module, "transition_run_status"),
            "transition_run_status is not implemented",
        )
        cases = (
            (state_module.RunStatus.READY, state_module.RunStatus.RUNNING),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.INTERRUPTED),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.FAILED),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.SUCCEEDED),
            (state_module.RunStatus.INTERRUPTED, state_module.RunStatus.RUNNING),
        )
        for current, target in cases:
            with self.subTest(current=current, target=target):
                self.assertEqual(
                    state_module.transition_run_status(current, target), target
                )

    def test_status_enum_values_are_exact_and_unique(self) -> None:
        self.assertTrue(
            hasattr(state_module, "RunStatus"), "RunStatus is not implemented"
        )
        self.assertEqual(
            tuple(member.value for member in state_module.RunStatus),
            ("READY", "RUNNING", "INTERRUPTED", "FAILED", "SUCCEEDED"),
        )
        self.assertEqual(
            tuple(member.value for member in state_module.StageStatus),
            ("PENDING", "RUNNING", "INTERRUPTED", "FAILED", "SUCCEEDED"),
        )

    def test_all_unlisted_run_transitions_are_rejected(self) -> None:
        self.assertTrue(
            hasattr(state_module, "RunStatus"), "RunStatus is not implemented"
        )
        self.assertTrue(
            hasattr(state_module, "transition_run_status"),
            "transition_run_status is not implemented",
        )
        legal = {
            (state_module.RunStatus.READY, state_module.RunStatus.RUNNING),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.INTERRUPTED),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.FAILED),
            (state_module.RunStatus.RUNNING, state_module.RunStatus.SUCCEEDED),
            (state_module.RunStatus.INTERRUPTED, state_module.RunStatus.RUNNING),
        }
        for current in state_module.RunStatus:
            for target in state_module.RunStatus:
                if (current, target) in legal:
                    continue
                with self.subTest(current=current, target=target):
                    with self.assertRaises(StateTransitionError):
                        state_module.transition_run_status(current, target)

    def test_all_unlisted_stage_transitions_are_rejected(self) -> None:
        legal = {
            (state_module.StageStatus.PENDING, state_module.StageStatus.RUNNING),
            (state_module.StageStatus.RUNNING, state_module.StageStatus.SUCCEEDED),
            (state_module.StageStatus.RUNNING, state_module.StageStatus.FAILED),
            (
                state_module.StageStatus.RUNNING,
                state_module.StageStatus.INTERRUPTED,
            ),
            (
                state_module.StageStatus.INTERRUPTED,
                state_module.StageStatus.RUNNING,
            ),
        }
        for current in state_module.StageStatus:
            for target in state_module.StageStatus:
                if (current, target) in legal:
                    continue
                original = state_module.StageState("build", current, 1, None, None)
                with self.subTest(current=current, target=target):
                    with self.assertRaises(StateTransitionError):
                        state_module.transition_stage_state(original, target)

    def test_reset_is_not_an_ordinary_run_transition(self) -> None:
        self.assertTrue(
            hasattr(state_module, "RunStatus"), "RunStatus is not implemented"
        )
        with self.assertRaises(StateTransitionError):
            state_module.transition_run_status(
                state_module.RunStatus.SUCCEEDED,
                state_module.RunStatus.READY,
            )

    def test_transition_error_is_invalid_state(self) -> None:
        self.assertTrue(issubclass(StateTransitionError, InvalidStateError))
        self.assertEqual(StateTransitionError.exit_code, ExitCode.INVALID_STATE)

    def test_initial_and_restarted_stage_attempts_are_pure(self) -> None:
        self.assertTrue(
            hasattr(state_module, "initial_stage_state"),
            "initial_stage_state is not implemented",
        )
        initial = state_module.initial_stage_state("build")
        self.assertEqual(
            initial,
            state_module.StageState(
                "build", state_module.StageStatus.PENDING, 0, None, None
            ),
        )
        first_start = state_module.transition_stage_state(
            initial, state_module.StageStatus.RUNNING
        )
        interrupted = state_module.transition_stage_state(
            first_start, state_module.StageStatus.INTERRUPTED
        )
        restarted = state_module.transition_stage_state(
            interrupted, state_module.StageStatus.RUNNING
        )
        self.assertEqual(
            (
                initial.attempts,
                first_start.attempts,
                interrupted.attempts,
                restarted.attempts,
            ),
            (0, 1, 1, 2),
        )
        self.assertEqual(initial.status, state_module.StageStatus.PENDING)
        self.assertEqual(interrupted.status, state_module.StageStatus.INTERRUPTED)


class StateValueTests(unittest.TestCase):
    def assert_schema(self, value_class, expected) -> None:
        self.assertEqual(
            tuple((field.name, field.type) for field in fields(value_class)),
            expected,
        )

    def test_state_and_result_dataclass_schemas_are_exact_and_frozen(self) -> None:
        required = ("StageContext", "StageResult", "RunResult", "RunStatus")
        self.assertEqual(
            tuple(name for name in required if not hasattr(state_module, name)),
            (),
            "contract result types are not implemented",
        )
        self.assert_schema(
            state_module.StageState,
            (
                ("stage_id", str),
                ("status", state_module.StageStatus),
                ("attempts", int),
                ("exit_code", int | None),
                ("output", JsonValue),
            ),
        )
        self.assert_schema(
            state_module.StageContext,
            (
                ("job_id", str),
                ("run_id", str),
                ("generation", int),
                ("stage_id", str),
                ("attempt", int),
            ),
        )
        self.assert_schema(
            state_module.StageResult,
            (("stage_id", str), ("exit_code", int), ("output", JsonValue)),
        )
        self.assert_schema(
            state_module.RunResult,
            (
                ("job_id", str),
                ("run_id", str),
                ("generation", int),
                ("status", state_module.RunStatus),
                ("durable_status", state_module.RunStatus),
                ("completed_stage_ids", tuple[str, ...]),
                ("failed_stage_id", str | None),
                ("exit_code", ExitCode),
                ("resumed", bool),
                ("checkpoint_revision", int),
            ),
        )
        values = (
            state_module.StageState(
                "build", state_module.StageStatus.PENDING, 0, None, None
            ),
            state_module.StageContext("job", "job:0", 0, "build", 1),
            state_module.StageResult("build", 0, None),
            state_module.RunResult(
                "job",
                "job:0",
                0,
                state_module.RunStatus.SUCCEEDED,
                state_module.RunStatus.SUCCEEDED,
                ("build",),
                None,
                ExitCode.OK,
                False,
                3,
            ),
        )
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, next(iter(value.__dict__)), "changed")


if __name__ == "__main__":
    unittest.main()
