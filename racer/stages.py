from typing import Protocol

from .job import StageSpec
from .state import StageContext, StageResult


class StageExecutor(Protocol):
    def execute(self, spec: StageSpec, context: StageContext) -> StageResult: ...


class FixtureStageExecutor:
    def execute(self, spec: StageSpec, context: StageContext) -> StageResult:
        if spec.action == "emit":
            return StageResult(spec.stage_id, 0, {"emitted": spec.payload})
        return StageResult(spec.stage_id, 1, {"error": spec.payload})
