from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Event, Thread
from uuid import uuid4

from .coordinator import LmFactory, RunProgress, StageCoordinator, default_lm_factory
from .planning import CompiledWorkspace
from .progress import ProgressEvent, ProgressSink
from .recorder import RecorderFactory, RecorderWorkerService
from .token_preflight import EndpointTokenPreflight


class _ManagedRecorderConsumer:
    """Consume one run's recorder queue inside the public run command."""

    def __init__(
        self,
        service: RecorderWorkerService,
        progress_sink: ProgressSink | None,
    ) -> None:
        self.service = service
        self.progress_sink = progress_sink
        self.worker_id = f"managed-recorder-{uuid4()}"
        self.run_id: str | None = None
        self.experiment_id: str | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def observe(self, event: ProgressEvent) -> None:
        if self.progress_sink is not None:
            self.progress_sink(event)
        if event.kind not in {"run_started", "run_resumed"} or self._thread is not None:
            return
        self.run_id = event.run_id
        self.experiment_id = event.experiment_id
        self._thread = Thread(
            target=self._consume,
            name=f"ms-agent-eval-recorder-{event.run_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        if self.progress_sink is not None:
            self.progress_sink(
                ProgressEvent(
                    kind="recording_sync_started",
                    run_id=event.run_id,
                    experiment_id=event.experiment_id,
                    details={"recorder": self.service.configuration.id},
                )
            )

    def close(self) -> None:
        if self._thread is None or self.run_id is None or self.experiment_id is None:
            return
        self._stop.set()
        self._thread.join()
        self.service.work_until_idle(
            worker_id=self.worker_id,
            run_id=self.run_id,
        )
        if self.service.recording.wait_on_run_exit:
            self._wait_for_terminal_delivery()
        if self.progress_sink is not None:
            self.progress_sink(
                ProgressEvent(
                    kind="recording_status",
                    run_id=self.run_id,
                    experiment_id=self.experiment_id,
                    details=self.service.status(self.run_id),
                )
            )

    def _consume(self) -> None:
        assert self.run_id is not None
        while not self._stop.is_set():
            result = self.service.work_once(
                worker_id=self.worker_id,
                run_id=self.run_id,
            )
            if result is None:
                self._stop.wait(0.25)

    def _wait_for_terminal_delivery(self) -> None:
        assert self.run_id is not None
        while True:
            status = self.service.status(self.run_id)
            queue = status.get("queue", {})
            if not isinstance(queue, Mapping):
                return
            if int(queue.get("pending", 0)) == 0 and int(queue.get("leased", 0)) == 0:
                return
            result = self.service.work_once(
                worker_id=self.worker_id,
                run_id=self.run_id,
            )
            if result is None:
                self._stop.wait(0.25)


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
        self.progress_sink = progress_sink
        self.coordinator = StageCoordinator(
            workspace,
            lm_factory=lm_factory,
            progress_sink=None,
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
        return self._execute(
            lambda: self.coordinator.start(
                experiment_id,
                environment=environment,
                through=through,
                pilot=pilot,
            )
        )

    def resume(
        self,
        run_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        return self._execute(
            lambda: self.coordinator.resume(
                run_id,
                environment=environment,
                through=through,
            )
        )

    def run_or_resume(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
        pilot: bool = False,
        fresh: bool = False,
    ) -> RunProgress:
        """Continue the matching unfinished revision unless a fresh run is explicit."""

        if not fresh:
            run_id = self.coordinator.resumable_run_id(
                experiment_id,
                environment=environment,
                pilot=pilot,
            )
            if run_id is not None:
                return self.resume(run_id, environment=environment, through=through)
        return self.run(
            experiment_id,
            environment=environment,
            through=through,
            pilot=pilot,
        )

    def _execute(self, operation: Callable[[], RunProgress]) -> RunProgress:
        recording = self.coordinator.recorder_service.recording
        if not recording.enabled:
            self.coordinator.progress_sink = self.progress_sink
            return operation()
        consumer = _ManagedRecorderConsumer(
            self.coordinator.recorder_service,
            self.progress_sink,
        )
        self.coordinator.progress_sink = consumer.observe
        try:
            return operation()
        finally:
            consumer.close()
            self.coordinator.progress_sink = self.progress_sink


__all__ = ["ExperimentRunner", "RunProgress"]
