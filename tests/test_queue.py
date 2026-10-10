import importlib
import unittest
from copy import deepcopy

from racer.errors import InvalidStateError
from racer.exit_codes import ExitCode
from racer.job import StageSpec
from racer.state import StageState, StageStatus


def stage(stage_id: str) -> StageSpec:
    return StageSpec(
        stage_id=stage_id,
        action="emit",
        payload=None,
        requirement_ids=("req",),
        interface_ids=("interface",),
        test_ids=("test",),
    )


def state(stage_id: str, status: StageStatus) -> StageState:
    return StageState(
        stage_id=stage_id,
        status=status,
        attempts=0,
        exit_code=None,
        output=None,
    )


class OrderedStageQueueTests(unittest.TestCase):
    def queue_module(self):
        try:
            return importlib.import_module("racer.queue")
        except ModuleNotFoundError as exc:
            self.fail(f"ordered stage queue is not implemented: {exc}")

    def test_queue_skips_succeeded_and_returns_first_pending(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("z_done"), stage("m_next"), stage("a_later"))
        states = {
            "a_later": state("a_later", StageStatus.PENDING),
            "m_next": state("m_next", StageStatus.PENDING),
            "z_done": state("z_done", StageStatus.SUCCEEDED),
        }

        result = queue_module.OrderedStageQueue(stages).next_stage(states)

        self.assertIs(result, stages[1])

    def test_queue_rejects_running_interrupted_and_failed(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("pending_first"), stage("blocked_later"))
        for blocked_status in (
            StageStatus.RUNNING,
            StageStatus.INTERRUPTED,
            StageStatus.FAILED,
        ):
            states = {
                "pending_first": state("pending_first", StageStatus.PENDING),
                "blocked_later": state("blocked_later", blocked_status),
            }
            with self.subTest(status=blocked_status):
                with self.assertRaises(InvalidStateError) as raised:
                    queue_module.OrderedStageQueue(stages).next_stage(states)
                self.assertEqual(raised.exception.exit_code, ExitCode.INVALID_STATE)
                self.assertIn("blocked_later", str(raised.exception))
                self.assertIn(blocked_status.value, str(raised.exception))

    def test_queue_rejects_missing_state(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("first"), stage("missing"))

        with self.assertRaises(InvalidStateError) as raised:
            queue_module.OrderedStageQueue(stages).next_stage(
                {"first": state("first", StageStatus.PENDING)}
            )

        self.assertEqual(raised.exception.exit_code, ExitCode.INVALID_STATE)
        self.assertIn("missing", str(raised.exception))

    def test_queue_rejects_extra_state(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("expected"),)
        states = {
            "expected": state("expected", StageStatus.PENDING),
            "extra": state("extra", StageStatus.PENDING),
        }

        with self.assertRaises(InvalidStateError) as raised:
            queue_module.OrderedStageQueue(stages).next_stage(states)

        self.assertEqual(raised.exception.exit_code, ExitCode.INVALID_STATE)
        self.assertIn("extra", str(raised.exception))

    def test_ordered_stage_ids_preserve_declaration_order_as_tuple(self) -> None:
        queue_module = self.queue_module()
        self.assertTrue(
            hasattr(queue_module, "StageQueue"), "StageQueue Protocol is missing"
        )
        queue = queue_module.OrderedStageQueue(
            [stage("z_first"), stage("a_second")]
        )

        result = queue.ordered_stage_ids()

        self.assertIs(type(result), tuple)
        self.assertEqual(result, ("z_first", "a_second"))
        self.assertTrue(hasattr(queue_module.StageQueue, "ordered_stage_ids"))
        self.assertTrue(hasattr(queue_module.StageQueue, "next_stage"))

    def test_mapping_insertion_order_does_not_affect_selection(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("z_first"), stage("a_second"))
        first_mapping = {
            "a_second": state("a_second", StageStatus.PENDING),
            "z_first": state("z_first", StageStatus.PENDING),
        }
        second_mapping = {
            "z_first": state("z_first", StageStatus.PENDING),
            "a_second": state("a_second", StageStatus.PENDING),
        }
        queue = queue_module.OrderedStageQueue(stages)

        self.assertIs(queue.next_stage(first_mapping), stages[0])
        self.assertIs(queue.next_stage(second_mapping), stages[0])

    def test_queue_returns_none_when_all_stages_succeeded(self) -> None:
        queue_module = self.queue_module()
        stages = (stage("first"), stage("second"))
        states = {
            item.stage_id: state(item.stage_id, StageStatus.SUCCEEDED)
            for item in stages
        }

        self.assertIsNone(queue_module.OrderedStageQueue(stages).next_stage(states))

    def test_queue_does_not_modify_inputs(self) -> None:
        queue_module = self.queue_module()
        stages = [stage("first"), stage("second")]
        states = {
            "first": state("first", StageStatus.SUCCEEDED),
            "second": StageState(
                stage_id="second",
                status=StageStatus.PENDING,
                attempts=0,
                exit_code=None,
                output={"items": [1, 2]},
            ),
        }
        stages_before = deepcopy(stages)
        states_before = deepcopy(states)

        queue_module.OrderedStageQueue(stages).next_stage(states)

        self.assertEqual(stages, stages_before)
        self.assertEqual(states, states_before)

    def test_queue_error_messages_are_deterministic(self) -> None:
        queue_module = self.queue_module()
        queue = queue_module.OrderedStageQueue(
            (stage("first"), stage("second"))
        )
        messages = []
        for _ in range(2):
            with self.assertRaises(InvalidStateError) as raised:
                queue.next_stage({})
            messages.append(str(raised.exception))

        self.assertEqual(messages[0], messages[1])
        self.assertIn("first", messages[0])
        self.assertIn("second", messages[0])


if __name__ == "__main__":
    unittest.main()
