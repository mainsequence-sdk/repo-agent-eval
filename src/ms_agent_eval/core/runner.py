from __future__ import annotations

from collections.abc import Mapping

from .coordinator import LmFactory, RunProgress, StageCoordinator, default_lm_factory
from .planning import CompiledWorkspace
from .progress import ProgressSink
from .recorder import RecorderFactory
from .token_preflight import EndpointTokenPreflight


class ExperimentRunner:
    """Public full-run entry point backed exclusively by the staged coordinator."""

    def __init__(
        self,
        workspace: CompiledWorkspace,
        *,
        lm_factory: LmFactory = default_lm_factory,
        progress_sink: ProgressSink | None = None,
        token_preflight: EndpointTokenPreflight | None = None,
        recorder_factory: RecorderFactory | None = None,
    ) -> None:
        self.coordinator = StageCoordinator(
            workspace,
            lm_factory=lm_factory,
            progress_sink=progress_sink,
            token_preflight=token_preflight,
            recorder_factory=recorder_factory,
        )

    def run(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
        pilot: bool = False,
    ) -> RunProgress:
        return self.coordinator.start(
            experiment_id,
            environment=environment,
            through=through,
            pilot=pilot,
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
