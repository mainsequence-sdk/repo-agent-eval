from __future__ import annotations

from collections.abc import Mapping

from .coordinator import LmFactory, RunProgress, StageCoordinator, default_lm_factory
from .planning import CompiledWorkspace


class ExperimentRunner:
    """Public full-run entry point backed exclusively by the staged coordinator."""

    def __init__(
        self,
        workspace: CompiledWorkspace,
        *,
        lm_factory: LmFactory = default_lm_factory,
    ) -> None:
        self.coordinator = StageCoordinator(workspace, lm_factory=lm_factory)

    def run(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        return self.coordinator.start(
            experiment_id,
            environment=environment,
            through=through,
        )

    def resume(
        self,
        run_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        return self.coordinator.resume(run_id, environment=environment, through=through)


__all__ = ["ExperimentRunner", "RunProgress"]
