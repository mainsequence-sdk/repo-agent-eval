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
        selected = report.get("selected_case_count", 0)
        self.console.print(
            f"Preflight [bold]{experiment}[/] · {selected} cases · [bold]0 generation calls[/]"
        )
        revision = report.get("experiment_revision_id")
        if isinstance(revision, str):
            self.console.print(
                f"[dim]Revision {experiment}@{revision.removeprefix('sha256:')[:12]}[/]"
            )

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


def _optional(value: object) -> str:
    return "-" if value is None else str(value)


__all__ = ["TerminalPreflightReporter"]
