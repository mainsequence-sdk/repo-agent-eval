from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich.console import Console
from rich.table import Table


class TerminalPreflightReporter:
    """Render a generation-free token and capacity diagnostic."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def render(self, report: Mapping[str, object]) -> None:
        status = str(report.get("status", "unknown"))
        color = {"fits": "green", "blocked": "red", "unknown": "yellow"}.get(status, "yellow")
        experiment = str(report.get("experiment", "experiment"))
        self.console.print(
            f"Preflight [bold]{experiment}[/] · {report.get('execution_mode', 'benchmark')} · "
            "[bold]0 generation calls[/]"
        )
        revision = report.get("experiment_revision_id")
        if isinstance(revision, str):
            self.console.print(
                f"[dim]Revision {experiment}@{revision.removeprefix('sha256:')[:12]}[/]"
            )
        workload = report.get("workload")
        if isinstance(workload, Mapping):
            self._render_workload(workload)

        stage_table = Table(title="Stage fit and usable concurrency")
        stage_table.add_column("Stage")
        stage_table.add_column("Requests", justify="right")
        stage_table.add_column("State")
        stage_table.add_column("Configured", justify="right")
        stage_table.add_column("Exact fit", justify="right")
        stage_table.add_column("Dispatchable", justify="right")
        stage_table.add_column("Waves", justify="right")
        stages = report.get("stages")
        if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes)):
            for raw_stage in stages:
                if not isinstance(raw_stage, Mapping):
                    continue
                state = str(raw_stage.get("status", raw_stage.get("state", "unknown")))
                state_color = {
                    "fits": "green",
                    "blocked": "red",
                    "unknown": "yellow",
                    "deferred": "yellow",
                    "not_applicable": "dim",
                }.get(state, "white")
                stage_table.add_row(
                    str(raw_stage.get("stage", "-")),
                    str(raw_stage.get("logical_request_count", 0)),
                    f"[{state_color}]{state.replace('_', ' ')}[/{state_color}]",
                    str(raw_stage.get("configured_capacity", 0)),
                    str(raw_stage.get("guaranteed_parallel_capacity", 0)),
                    str(raw_stage.get("dispatchable_parallel_capacity", 0)),
                    _optional(raw_stage.get("minimum_dispatch_waves")),
                )
        self.console.print()
        self.console.print(stage_table)

        if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes)):
            for raw_stage in stages:
                if not isinstance(raw_stage, Mapping):
                    continue
                endpoints = raw_stage.get("endpoints")
                if (
                    not isinstance(endpoints, Sequence)
                    or isinstance(endpoints, (str, bytes))
                    or not endpoints
                ):
                    detail = raw_stage.get("detail")
                    if isinstance(detail, str):
                        self.console.print(f"[dim]{raw_stage.get('stage', 'stage')}: {detail}[/]")
                    continue
                table = Table(title=f"{raw_stage.get('stage')} endpoints")
                table.add_column("Endpoint")
                table.add_column("Slots", justify="right")
                table.add_column("Context", justify="right")
                table.add_column("Fits", justify="right")
                table.add_column("Exceeds", justify="right")
                table.add_column("Unknown", justify="right")
                table.add_column("Largest request", justify="right")
                for endpoint in endpoints:
                    if not isinstance(endpoint, Mapping):
                        continue
                    table.add_row(
                        str(endpoint.get("id", "-")),
                        str(endpoint.get("capacity", 0)),
                        _optional(endpoint.get("context_window")),
                        str(endpoint.get("fits", 0)),
                        str(endpoint.get("exceeds", 0)),
                        str(endpoint.get("skipped", 0)),
                        _optional(endpoint.get("maximum_required_tokens")),
                    )
                self.console.print()
                self.console.print(table)

        summary = report.get("summary")
        if isinstance(summary, Mapping):
            blocked = int(summary.get("blocked_requests", 0))
            unknown = int(summary.get("unknown_only_requests", 0))
            if blocked:
                self.console.print(
                    f"[red]Blocked: {blocked} request(s) exceed every endpoint.[/red]"
                )
            elif unknown:
                self.console.print(
                    f"[yellow]Unknown: {unknown} request(s) have no endpoint with an exact "
                    "native token result; runtime dispatch remains allowed.[/yellow]"
                )
            elif status == "deferred":
                self.console.print(
                    "[yellow]No selected stage has a static final request to calculate yet.[/]"
                )
            else:
                self.console.print(
                    f"[{color}]All statically available requests have an exact-fit route.[/{color}]"
                )

    def _render_workload(self, workload: Mapping[str, object]) -> None:
        inventory = workload.get("case_inventory")
        if isinstance(inventory, Mapping):
            table = Table(title="Case sets used by this experiment")
            table.add_column("Set")
            table.add_column("Cases", justify="right")
            table.add_column("Purpose")
            permanent = inventory.get("permanent_splits")
            if isinstance(permanent, Mapping):
                purposes = {
                    "train": "source for optimization training and validation",
                    "test": "untouched final evaluation",
                    "challenge": "retained; not used by this run",
                }
                for split in ("train", "test", "challenge"):
                    table.add_row(split, str(permanent.get(split, 0)), purposes[split])
            derived = inventory.get("derived")
            if isinstance(derived, Mapping):
                table.add_row(
                    "optimization training",
                    str(derived.get("optimization_training_cases", 0)),
                    "fits candidates; never used for final scoring",
                )
                table.add_row(
                    "validation",
                    str(derived.get("validation_cases", 0)),
                    "selects candidates; never used for final scoring",
                )
            execution = inventory.get("execution")
            if isinstance(execution, Mapping):
                arms = execution.get("arms", 1)
                table.add_row(
                    "executed final set",
                    str(execution.get("case_count", 0)),
                    f"{execution.get('split', 'test')} across {arms} arm(s)",
                )
            self.console.print()
            self.console.print(table)

        steps = workload.get("steps")
        if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
            table = Table(title="Expanded fresh-run workload")
            table.add_column("Stage")
            table.add_column("Phase")
            table.add_column("Role")
            table.add_column("Units", justify="right")
            table.add_column("×", justify="right")
            table.add_column("Calls / operations", justify="right")
            table.add_column("Expansion")
            for raw_step in steps:
                if not isinstance(raw_step, Mapping):
                    continue
                model = raw_step.get("kind") == "model"
                table.add_row(
                    str(raw_step.get("stage", "-")),
                    str(raw_step.get("phase", "-")),
                    str(raw_step.get("role", "local")),
                    _optional(raw_step.get("units")),
                    _optional(raw_step.get("multiplicity")),
                    str(raw_step.get("calls" if model else "operations", 0)),
                    str(raw_step.get("formula", "")),
                )
            self.console.print()
            self.console.print(table)

        partitions = workload.get("partitions")
        if (
            isinstance(partitions, Sequence)
            and not isinstance(partitions, (str, bytes))
            and partitions
        ):
            table = Table(title="Stateful COPRO training partitions")
            table.add_column("Partition", justify="right")
            table.add_column("Train cases", justify="right")
            table.add_column("Groups", justify="right")
            table.add_column("Targets", justify="right")
            table.add_column("Rounds", justify="right")
            table.add_column("Candidates", justify="right")
            table.add_column("Optimizer", justify="right")
            table.add_column("Solver", justify="right")
            table.add_column("Judge", justify="right")
            for raw_partition in partitions:
                if not isinstance(raw_partition, Mapping):
                    continue
                table.add_row(
                    str(raw_partition.get("partition", "-")),
                    str(raw_partition.get("training_cases", 0)),
                    str(raw_partition.get("training_groups", 0)),
                    str(raw_partition.get("target_count", 0)),
                    str(raw_partition.get("rounds", 0)),
                    str(raw_partition.get("candidates_per_target", 0)),
                    str(raw_partition.get("optimizer_calls", 0)),
                    str(raw_partition.get("candidate_solver_calls", 0)),
                    str(raw_partition.get("candidate_judge_calls", 0)),
                )
            self.console.print()
            self.console.print(table)
            self.console.print(
                "[dim]Each partition starts from the previous partition's selected prompt/skills "
                "and score history.[/]"
            )

        routes = workload.get("model_routes")
        if isinstance(routes, Sequence) and not isinstance(routes, (str, bytes)):
            table = Table(title="Models and configured execution capacity")
            table.add_column("Role")
            table.add_column("Model")
            table.add_column("Deployment")
            table.add_column("Executor")
            table.add_column("Slots", justify="right")
            table.add_column("Endpoints")
            for raw_route in routes:
                if not isinstance(raw_route, Mapping):
                    continue
                endpoints = raw_route.get("endpoints")
                endpoint_names = []
                if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
                    endpoint_names = [
                        f"{item.get('id')} ({item.get('capacity')})"
                        for item in endpoints
                        if isinstance(item, Mapping)
                    ]
                table.add_row(
                    str(raw_route.get("role", "-")),
                    str(raw_route.get("model", "-")),
                    str(raw_route.get("deployment", "-")),
                    str(raw_route.get("executor", "-")),
                    _optional(raw_route.get("configured_capacity")),
                    ", ".join(endpoint_names) or str(raw_route.get("adapter", "-")),
                )
            self.console.print()
            self.console.print(table)

        totals = workload.get("totals")
        if isinstance(totals, Mapping):
            by_role = totals.get("by_role")
            roles = (
                " · ".join(f"{key} {value}" for key, value in by_role.items())
                if isinstance(by_role, Mapping)
                else ""
            )
            self.console.print()
            self.console.print(
                "Fresh upper bound: "
                f"[bold]{totals.get('logical_model_calls', 0)} model calls[/]"
                + (f" ({roles})" if roles else "")
                + f" · {totals.get('local_operations', 0)} local operations"
            )
            note = workload.get("reuse_note")
            if isinstance(note, str):
                self.console.print(f"[dim]{note}[/]")


def _optional(value: object) -> str:
    return "-" if value is None else str(value)


__all__ = ["TerminalPreflightReporter"]
