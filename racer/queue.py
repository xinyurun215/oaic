from collections.abc import Mapping, Sequence
from typing import Protocol

from .errors import InvalidStateError
from .job import StageSpec
from .state import StageState, StageStatus


class StageQueue(Protocol):
    def ordered_stage_ids(self) -> tuple[str, ...]: ...

    def next_stage(
        self,
        states: Mapping[str, StageState],
    ) -> StageSpec | None: ...


class OrderedStageQueue:
    def __init__(self, stages: Sequence[StageSpec]) -> None:
        self._stages = tuple(stages)

    def ordered_stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.stage_id for stage in self._stages)

    def next_stage(
        self,
        states: Mapping[str, StageState],
    ) -> StageSpec | None:
        expected_ids = tuple(stage.stage_id for stage in self._stages)
        expected_id_set = set(expected_ids)
        missing_ids = tuple(
            stage_id for stage_id in expected_ids if stage_id not in states
        )
        if missing_ids:
            raise InvalidStateError(
                f"missing stage states: {', '.join(missing_ids)}"
            )

        extra_ids = tuple(sorted(set(states) - expected_id_set))
        if extra_ids:
            raise InvalidStateError(f"extra stage states: {', '.join(extra_ids)}")

        for stage in self._stages:
            stage_state = states[stage.stage_id]
            if stage_state.status in (
                StageStatus.RUNNING,
                StageStatus.INTERRUPTED,
                StageStatus.FAILED,
            ):
                raise InvalidStateError(
                    f"stage '{stage.stage_id}' has invalid queue status "
                    f"{stage_state.status.value}"
                )

        for stage in self._stages:
            if states[stage.stage_id].status is StageStatus.PENDING:
                return stage
        return None
