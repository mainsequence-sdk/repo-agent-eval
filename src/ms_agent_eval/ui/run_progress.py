from __future__ import annotations

from collections.abc import Mapping

from rich.console import Console
from rich.status import Status
from rich.table import Table

from ms_agent_eval.core.progress import ProgressEvent


_STAGE_LABELS = {
    "author": ("Authoring cases", "Authoring"),
    "calibrate_judge": ("Calibrating judge", "Calibrating"),
    "solve": ("Solving cases", "Solving"),
    "judge": ("Judging responses", "Judging"),
    "optimize": ("Optimizing DSPy program", "Optimizing"),
    "final_evaluate": ("Opening held-out evaluation", "Validating"),
    "aggregate": ("Aggregating results", "Aggregating"),
}

_STAGE_SUBJECTS = {
    "author": "case campaign",
    "calibrate_judge": "judge calibration",
    "optimize": "DSPy program",
    "final_evaluate": "held-out evaluation gate",
    "aggregate": "results",
}


class TerminalRunReporter:
    """Render coordinator events for a person without changing event semantics."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._status: Status | None = None
        self._last_stage: str | None = None

    def handle(self, event: ProgressEvent) -> None:
        if event.kind in {"run_started", "run_resumed"}:
            self._run_header(event)
        elif event.kind == "packet_started":
            self._packet_started(event)
        elif event.kind == "packet_completed":
            self._packet_completed(event)
        elif event.kind == "packet_reused":
            self._packet_reused(event)
        elif event.kind == "packet_failed":
            self._packet_failed(event)
        elif event.kind == "packet_waiting":
            self._packet_waiting(event)
        elif event.kind == "cleanup_completed":
            self._cleanup(event)
        elif event.kind == "run_status":
            self._run_status(event)

    def close(self) -> None:
        self._stop_status()

    def _run_header(self, event: ProgressEvent) -> None:
        action = "Resuming" if event.kind == "run_resumed" else "Starting"
        mode = str(event.details.get("execution_mode", "benchmark"))
        case_count = event.details.get("case_count")
        description = f"{action} [bold]{event.experiment_id}[/] · {mode}"
        if isinstance(case_count, int):
            description += f" · {case_count} cases"
        self.console.print(description)
        self.console.print(f"[dim]Run {event.run_id}[/]")
        revision = event.details.get("experiment_revision_id")
        if isinstance(revision, str):
            self.console.print(
                f"[dim]Revision {event.experiment_id}@{revision.removeprefix('sha256:')[:12]}[/]"
            )

    def _packet_started(self, event: ProgressEvent) -> None:
        self._stop_status()
        stage = event.stage or "stage"
        if stage != self._last_stage:
            self.console.print()
            self.console.rule(_STAGE_LABELS.get(stage, (stage, stage))[0], style="cyan")
            self._last_stage = stage
        verb = _STAGE_LABELS.get(stage, (stage, stage))[1]
        subject = _subject(event)
        self._status = self.console.status(
            f"[cyan]{verb}[/] {subject}{_start_detail(event)}{_model_suffix(event)}",
            spinner="dots",
        )
        self._status.start()

    def _packet_completed(self, event: ProgressEvent) -> None:
        self._stop_status()
        elapsed = _duration(event)
        detail = _completion_detail(event)
        self.console.print(
            f"[green]✓[/] {_past_tense(event.stage)} {_subject(event)}{detail}{elapsed}"
        )
        authored_ids = event.details.get("case_ids")
        if isinstance(authored_ids, list) and authored_ids:
            self.console.print("  [dim]" + ", ".join(str(item) for item in authored_ids) + "[/]")

    def _packet_reused(self, event: ProgressEvent) -> None:
        self._stop_status()
        source_run = event.details.get("source_run_id")
        suffix = f" [dim]· from {source_run}[/]" if isinstance(source_run, str) else ""
        self.console.print(
            f"[blue]↺[/] Reused {_subject(event)}{_completion_detail(event)}{suffix}"
        )

    def _packet_failed(self, event: ProgressEvent) -> None:
        self._stop_status()
        message = event.details.get("error")
        suffix = f": {message}" if isinstance(message, str) and message else ""
        self.console.print(f"[red]✗[/] {_subject(event)} failed{suffix}")

    def _packet_waiting(self, event: ProgressEvent) -> None:
        self._stop_status()
        self.console.print(f"[yellow]●[/] {_subject(event)} is ready for harness execution")

    def _cleanup(self, event: ProgressEvent) -> None:
        blobs = event.details.get("deleted_blobs", 0)
        temporary = event.details.get("deleted_temporary", 0)
        self.console.print(
            f"[dim]Storage cleanup · {blobs} blobs and {temporary} temporary entries removed[/]"
        )

    def _run_status(self, event: ProgressEvent) -> None:
        self._stop_status()
        status = str(event.details.get("status", "unknown"))
        if status in {"completed", "pilot_completed"}:
            result = event.details.get("result")
            self._summary(event, result if isinstance(result, Mapping) else {})
            return
        labels = {
            "paused": "Paused",
            "awaiting_execution": "Awaiting external execution",
            "calibration_failed": "Judge calibration failed",
        }
        color = "red" if status == "calibration_failed" else "yellow"
        label = labels.get(status, status.replace("_", " ").title())
        stage = f" at {event.stage}" if event.stage else ""
        self.console.print(f"[{color}]{label}{stage}[/{color}]")

    def _summary(self, event: ProgressEvent, result: Mapping[str, object]) -> None:
        table = Table(title="Experiment complete", show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Run", event.run_id)
        table.add_row("Status", str(event.details.get("status")))
        table.add_row("Cases", str(result.get("case_count", 0)))
        if isinstance(result.get("mean_score"), (int, float)):
            table.add_row("Mean score", f"{float(result['mean_score']):.3f}")
        if isinstance(result.get("pass_rate"), (int, float)):
            table.add_row("Pass rate", f"{float(result['pass_rate']):.1%}")
        if result.get("diagnostic_only") is True:
            table.add_row("Metrics", "Diagnostic only (pilot)")
        execution = result.get("execution")
        if isinstance(execution, Mapping):
            table.add_row(
                "Work",
                f"{execution.get('executed_this_run', 0)} executed · "
                f"{execution.get('reused_this_run', 0)} reused",
            )
        data_root = event.details.get("data_root")
        if isinstance(data_root, str):
            table.add_row("Data", data_root)
        self.console.print()
        self.console.print(table)

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


def _subject(event: ProgressEvent) -> str:
    progress = ""
    if event.position is not None and event.total is not None:
        progress = f"[{event.position}/{event.total}] "
    if event.case_id is None:
        return f"{progress}{_STAGE_SUBJECTS.get(event.stage or '', 'stage')}"
    title = event.case_title or event.case_id
    metadata = [event.case_id]
    if event.skill:
        metadata.append(event.skill)
    if event.split:
        metadata.append(event.split)
    if event.repetition and event.repetition > 1:
        metadata.append(f"repeat {event.repetition}")
    return f"{progress}[bold]{title}[/] [dim]({' · '.join(metadata)})[/]"


def _model_suffix(event: ProgressEvent) -> str:
    return f" [dim]· {event.model}[/]" if event.model else ""


def _start_detail(event: ProgressEvent) -> str:
    fixtures = event.details.get("fixture_count")
    if event.stage == "calibrate_judge" and isinstance(fixtures, int):
        return f" [dim]· {fixtures} fixtures[/]"
    training_cases = event.details.get("training_case_count")
    if event.stage == "optimize" and isinstance(training_cases, int):
        return f" [dim]· {training_cases} training cases[/]"
    return ""


def _duration(event: ProgressEvent) -> str:
    if event.duration_seconds is None:
        return ""
    return f" [dim]· {event.duration_seconds:.1f}s[/]"


def _completion_detail(event: ProgressEvent) -> str:
    if event.stage == "judge" and isinstance(event.details.get("score"), (int, float)):
        passed = event.details.get("passed") is True
        verdict = "passed" if passed else "failed"
        color = "green" if passed else "red"
        return f" · [{color}]{float(event.details['score']):.3f} {verdict}[/{color}]"
    if event.stage == "calibrate_judge":
        passed = event.details.get("passed") is True
        return " · judge accepted" if passed else " · judge rejected"
    if event.stage == "author" and isinstance(event.details.get("produced_count"), int):
        return f" · {event.details['produced_count']} candidates"
    return ""


def _past_tense(stage: str | None) -> str:
    return {
        "author": "Authored",
        "calibrate_judge": "Calibrated",
        "solve": "Solved",
        "judge": "Judged",
        "optimize": "Optimized",
        "final_evaluate": "Validated",
        "aggregate": "Aggregated",
    }.get(stage or "", "Completed")


__all__ = ["TerminalRunReporter"]
