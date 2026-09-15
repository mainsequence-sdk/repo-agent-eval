from __future__ import annotations

from collections.abc import Mapping

from rich.console import Console
from rich.markup import escape
from rich.status import Status
from rich.table import Table

from ms_agent_eval.core.progress import ProgressEvent


_STAGE_LABELS = {
    "author": ("Authoring cases", "Authoring"),
    "validate_judge": ("Validating judge ranking", "Validating"),
    "optimize": ("Proposing COPRO candidates", "Proposing"),
    "solve": ("Solving cases", "Solving"),
    "judge": ("Judging responses", "Judging"),
    "fit": ("Fitting DSPy programs", "Fitting"),
    "select": ("Selecting on validation cases", "Selecting"),
    "aggregate": ("Aggregating results", "Aggregating"),
}

_STAGE_SUBJECTS = {
    "author": "case campaign",
    "validate_judge": "judge validation",
    "optimize": "optimizer candidate set",
    "fit": "DSPy program",
    "select": "validation candidate",
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
        elif event.kind == "packet_retrying":
            self._packet_retrying(event)
        elif event.kind == "packet_failed":
            self._packet_failed(event)
        elif event.kind == "packet_waiting":
            self._packet_waiting(event)
        elif event.kind in {
            "judge_validation_vote_completed",
            "judge_validation_vote_failed",
        }:
            self._judge_validation_vote(event)
        elif event.kind == "cleanup_completed":
            self._cleanup(event)
        elif event.kind == "run_status":
            self._run_status(event)
        elif event.kind == "recording_status":
            self._recording_status(event)
        elif event.kind == "recording_sync_started":
            self._recording_sync_started(event)

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
        optimization = event.details.get("optimization")
        if isinstance(optimization, Mapping):
            strategy = optimization.get("strategy")
            self.console.print(f"[dim]Optimization strategy: {strategy}[/]")
            if strategy == "copro":
                search_space = optimization.get("search_space_type")
                targets = (
                    optimization.get("skills", "configured skills")
                    if search_space == "skills"
                    else optimization.get(
                        "global_instruction_files", "configured global instructions"
                    )
                )
                self.console.print(
                    "[dim]COPRO search: "
                    f"breadth {optimization.get('breadth')} × depth {optimization.get('depth')} "
                    f"· search space {search_space} · selection {targets}[/]"
                )
        workload = event.details.get("workload")
        if isinstance(workload, Mapping):
            self._workload_summary(workload)

    def _workload_summary(self, workload: Mapping[str, object]) -> None:
        inventory = workload.get("case_inventory")
        if isinstance(inventory, Mapping):
            permanent = inventory.get("permanent_splits")
            derived = inventory.get("derived")
            execution = inventory.get("execution")
            if isinstance(permanent, Mapping):
                detail = (
                    f"train {permanent.get('train', 0)} · test {permanent.get('test', 0)} · "
                    f"challenge {permanent.get('challenge', 0)}"
                )
                if isinstance(derived, Mapping):
                    detail += (
                        " · derived optimization training "
                        f"{derived.get('optimization_training_cases', 0)} + validation "
                        f"{derived.get('validation_cases', 0)}"
                    )
                if isinstance(execution, Mapping):
                    detail += f" · final arms {execution.get('arms', 1)}"
                self.console.print(f"[dim]Case plan: {detail}[/]")
        totals = workload.get("totals")
        if isinstance(totals, Mapping):
            by_role = totals.get("by_role")
            roles = (
                " · ".join(f"{role} {calls}" for role, calls in by_role.items())
                if isinstance(by_role, Mapping)
                else ""
            )
            self.console.print(
                f"[bold]Fresh upper bound: {totals.get('logical_model_calls', 0)} model calls[/]"
                + (f" [dim]· {roles}[/]" if roles else "")
            )
        search = workload.get("copro_search")
        if isinstance(search, Mapping):
            self.console.print(
                "[dim]COPRO search: "
                f"{search.get('training_cases')} training cases · "
                f"{search.get('target_count')} targets · "
                f"{search.get('rounds')} rounds · "
                f"{search.get('candidate_solver_calls')} validation solves[/]"
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
        description = f"[cyan]{verb}[/] {subject}{_start_detail(event)}{_model_suffix(event)}"
        if not self.console.is_terminal:
            self.console.print(description)
            return
        self._status = self.console.status(description, spinner="dots")
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

    def _packet_retrying(self, event: ProgressEvent) -> None:
        self._stop_status()
        attempt = event.details.get("attempt")
        suffix = f" · attempt {attempt}" if isinstance(attempt, int) else ""
        self.console.print(f"[yellow]↻[/] Retrying {_subject(event)}{suffix}")

    def _packet_failed(self, event: ProgressEvent) -> None:
        self._stop_status()
        if event.details.get("error_kind") == "context_window_exceeded":
            required = event.details.get("required_tokens")
            available = event.details.get("context_window")
            counts = (
                f" ({required} required > {available} available)"
                if isinstance(required, int) and isinstance(available, int)
                else ""
            )
            self.console.print(
                f"[red]✗[/] {_subject(event)} context preflight rejected request{counts}"
            )
            return
        message = event.details.get("error")
        rendered = _terminal_error(message)
        suffix = f": {rendered}" if rendered else ""
        self.console.print(f"[red]✗[/] {_subject(event)} failed{suffix}")

    def _packet_waiting(self, event: ProgressEvent) -> None:
        self._stop_status()
        self.console.print(f"[yellow]●[/] {_subject(event)} is ready for harness execution")

    def _judge_validation_vote(self, event: ProgressEvent) -> None:
        self._stop_status()
        failed = event.kind == "judge_validation_vote_failed"
        fixture = str(event.details.get("fixture_id", "fixture"))
        vote = event.details.get("vote")
        endpoint = event.details.get("endpoint_id")
        progress = (
            f"[{event.position}/{event.total}] "
            if event.position is not None and event.total is not None
            else ""
        )
        description = f"{progress}{fixture}"
        if isinstance(vote, int):
            description += f" · vote {vote}"
        if isinstance(endpoint, str):
            description += f" · {endpoint}"
        if event.details.get("preflight_status") == "skipped":
            description += " · preflight skipped"
        if failed:
            error = event.details.get("error")
            rendered = _terminal_error(error)
            if rendered:
                description += f": {rendered}"
        mark = "[red]✗[/]" if failed else "[green]✓[/]"
        self.console.print(f"{mark} {description}{_duration(event)}")

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
            "execution_failed": "Automatic execution failed",
            "judge_validation_failed": "Judge validation failed",
        }
        color = (
            "red"
            if status in {"execution_failed", "judge_validation_failed", "report_failed"}
            else "yellow"
        )
        label = labels.get(status, status.replace("_", " ").title())
        stage = f" at {event.stage}" if event.stage else ""
        error = event.details.get("result", {})
        message = error.get("error") if isinstance(error, Mapping) else None
        rendered = _terminal_error(message)
        suffix = f": {rendered}" if rendered else ""
        self.console.print(f"[{color}]{label}{stage}{suffix}[/{color}]")

    def _recording_status(self, event: ProgressEvent) -> None:
        self._stop_status()
        status = str(event.details.get("status", "unknown"))
        recorder = str(event.details.get("recorder", "recorder"))
        external = event.details.get("external_run_id")
        if status == "published":
            suffix = f" · run {external}" if isinstance(external, str) else ""
            self.console.print(f"[green]Recorded in {recorder}{suffix}[/green]")
            return
        queue = event.details.get("queue", {})
        pending = queue.get("pending") if isinstance(queue, Mapping) else None
        suffix = f" · {pending} queued" if isinstance(pending, int) else ""
        self.console.print(
            f"[yellow]Recording {status} in {recorder}{suffix}; "
            "automatic delivery remains safely retryable[/yellow]"
        )

    def _recording_sync_started(self, event: ProgressEvent) -> None:
        self._stop_status()
        recorder = str(event.details.get("recorder", "recorder"))
        description = f"[cyan]Recording experiment evidence in {recorder}…[/cyan]"
        if not self.console.is_terminal:
            self.console.print(description)
            return
        self._status = self.console.status(description, spinner="dots")
        self._status.start()

    def _summary(self, event: ProgressEvent, result: Mapping[str, object]) -> None:
        table = Table(title="Experiment complete", show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Run", event.run_id)
        table.add_row("Status", str(event.details.get("status")))
        table.add_row("Cases", str(result.get("case_count", 0)))
        if isinstance(result.get("mean_score"), (int, float)):
            table.add_row("Mean score", f"{float(result['mean_score']):.3f}")
        if result.get("diagnostic_only") is True:
            table.add_row("Metrics", "Diagnostic only (pilot)")
        execution = result.get("execution")
        if isinstance(execution, Mapping):
            table.add_row(
                "Work",
                f"{execution.get('executed_this_run', 0)} executed · "
                f"{execution.get('reused_this_run', 0)} reused",
            )
        endpoint_execution = result.get("endpoint_execution")
        if isinstance(endpoint_execution, Mapping):
            endpoints = endpoint_execution.get("endpoints")
            if isinstance(endpoints, list) and endpoints:
                calls = sum(
                    int(item.get("model_calls", 0))
                    for item in endpoints
                    if isinstance(item, Mapping)
                )
                table.add_row(
                    "Endpoint pool",
                    f"{len(endpoints)} replicas · "
                    f"{endpoint_execution.get('configured_capacity', 0)} slots · "
                    f"{calls} calls",
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
    details: list[str] = []
    fixtures = event.details.get("fixture_count")
    if event.stage == "validate_judge" and isinstance(fixtures, int):
        details.append(f"{fixtures} fixtures")
    requests = event.details.get("request_count")
    if event.stage == "validate_judge" and isinstance(requests, int):
        details.append(f"{requests} votes")
    capacity = event.details.get("pool_capacity")
    if isinstance(capacity, int):
        details.append(f"{capacity} slots")
    training_cases = event.details.get("training_case_count")
    if event.stage == "fit" and isinstance(training_cases, int):
        details.append(f"{training_cases} training cases")
    endpoint = event.details.get("endpoint_id")
    if isinstance(endpoint, str):
        details.append(f"endpoint {endpoint}")
    return "" if not details else f" [dim]· {' · '.join(details)}[/]"


def _duration(event: ProgressEvent) -> str:
    if event.duration_seconds is None:
        return ""
    return f" [dim]· {event.duration_seconds:.1f}s[/]"


def _terminal_error(value: object, *, maximum: int = 300) -> str:
    """Render one bounded line; full diagnostics belong in immutable artifacts."""

    if not isinstance(value, str) or not value.strip():
        return ""
    message = " ".join(value.split())
    if len(message) > maximum:
        message = message[: maximum - 3].rstrip() + "..."
    return escape(message)


def _completion_detail(event: ProgressEvent) -> str:
    detail = ""
    if event.stage == "judge" and isinstance(event.details.get("score"), (int, float)):
        detail = f" · [green]{float(event.details['score']):.3f}[/green]"
    elif event.stage == "validate_judge":
        consistent = event.details.get("ordering_consistent") is True
        detail = " · ordering consistent" if consistent else " · ordering inconsistent"
    elif event.stage == "author" and isinstance(event.details.get("produced_count"), int):
        detail = f" · {event.details['produced_count']} candidates"
    endpoint = event.details.get("endpoint_id")
    if isinstance(endpoint, str):
        detail += f" [dim]· {endpoint}[/]"
    endpoint_ids = event.details.get("endpoint_ids")
    if isinstance(endpoint_ids, list) and endpoint_ids:
        detail += f" [dim]· endpoints {', '.join(map(str, endpoint_ids))}[/]"
    preflight = event.details.get("preflight_status")
    if preflight == "skipped":
        detail += " [dim]· preflight skipped[/]"
    rejected = event.details.get("preflight_rejections")
    if isinstance(rejected, int) and rejected:
        detail += f" [dim]· {rejected} preflight reroutes[/]"
    return detail


def _past_tense(stage: str | None) -> str:
    return {
        "author": "Authored",
        "validate_judge": "Validated",
        "optimize": "Proposed",
        "solve": "Solved",
        "judge": "Judged",
        "fit": "Fitted",
        "select": "Selected",
        "aggregate": "Aggregated",
    }.get(stage or "", "Completed")


__all__ = ["TerminalRunReporter"]
