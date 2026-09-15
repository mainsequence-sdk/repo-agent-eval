from __future__ import annotations

from io import StringIO

from rich.console import Console

from ms_agent_eval.core.cli import _human_preflight, _human_progress, _parser
from ms_agent_eval.core.progress import ProgressEvent
from ms_agent_eval.ui import TerminalPreflightReporter, TerminalRunReporter


def test_terminal_reporter_names_cases_scores_and_final_summary() -> None:
    output = StringIO()
    reporter = TerminalRunReporter(
        Console(file=output, force_terminal=False, color_system=None, width=120)
    )
    common = {
        "run_id": "run-123",
        "experiment_id": "baseline",
    }
    reporter.handle(
        ProgressEvent(
            "run_started",
            **common,
            details={"execution_mode": "pilot", "case_count": 2},
        )
    )
    reporter.handle(
        ProgressEvent(
            "packet_started",
            **common,
            stage="judge",
            case_id="case-one",
            case_title="Evaluate one thing",
            skill="example-skill",
            split="train",
            position=1,
            total=2,
            model="judge-model",
        )
    )
    reporter.handle(
        ProgressEvent(
            "packet_completed",
            **common,
            stage="judge",
            case_id="case-one",
            case_title="Evaluate one thing",
            skill="example-skill",
            split="train",
            position=1,
            total=2,
            duration_seconds=1.25,
            details={"score": 0.875},
        )
    )
    reporter.handle(
        ProgressEvent(
            "cleanup_completed",
            **common,
            stage="aggregate",
            details={"deleted_blobs": 2, "deleted_temporary": 1},
        )
    )
    reporter.handle(
        ProgressEvent(
            "run_status",
            **common,
            stage="aggregate",
            details={
                "status": "pilot_completed",
                "data_root": "/data/evaluation",
                "result": {"case_count": 2, "diagnostic_only": True},
            },
        )
    )
    reporter.close()

    rendered = output.getvalue()
    assert "baseline · pilot · 2 cases" in rendered
    assert "[1/2] Evaluate one thing" in rendered
    assert "case-one · example-skill · train" in rendered
    assert "0.875" in rendered
    assert "passed" not in rendered
    assert "2 blobs and 1 temporary entries removed" in rendered
    assert "Experiment complete" in rendered
    assert "Diagnostic only (pilot)" in rendered


def test_json_flag_disables_live_progress_in_a_tty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    human = _parser().parse_args(["run", "baseline"])
    machine = _parser().parse_args(["run", "baseline", "--json"])

    assert _human_progress(human) is True
    assert _human_progress(machine) is False


def test_terminal_preflight_reporter_shows_fit_and_capacity() -> None:
    output = StringIO()
    reporter = TerminalPreflightReporter(
        Console(file=output, force_terminal=False, color_system=None, width=120)
    )
    reporter.render(
        {
            "status": "fits",
            "experiment": "baseline",
            "execution_mode": "benchmark",
            "experiment_revision_id": "sha256:1234567890abcdef",
            "selected_case_count": 10,
            "workload": {
                "case_inventory": {
                    "permanent_splits": {"train": 20, "test": 10, "challenge": 5},
                    "derived": None,
                    "execution": {"split": "test", "case_count": 10, "arms": 1},
                },
                "steps": [
                    {
                        "kind": "model",
                        "stage": "solve",
                        "phase": "test",
                        "role": "solver",
                        "units": 10,
                        "multiplicity": 1,
                        "calls": 10,
                        "formula": "cases × solver repetitions",
                    }
                ],
                "copro_search": None,
                "model_routes": [
                    {
                        "role": "solver",
                        "model": "model-a",
                        "deployment": "deployment-a",
                        "executor": "pool-a",
                        "configured_capacity": 4,
                        "endpoints": [{"id": "local-a", "capacity": 4}],
                    }
                ],
                "totals": {
                    "logical_model_calls": 10,
                    "by_role": {"solver": 10},
                    "local_operations": 1,
                },
                "reuse_note": "Fresh upper bound before reuse.",
            },
            "stages": [
                {
                    "stage": "solve",
                    "status": "fits",
                    "logical_request_count": 10,
                    "configured_capacity": 8,
                    "guaranteed_parallel_capacity": 6,
                    "dispatchable_parallel_capacity": 8,
                    "minimum_dispatch_waves": 2,
                    "endpoints": [
                        {
                            "id": "local-a",
                            "capacity": 4,
                            "context_window": 32768,
                            "fits": 10,
                            "exceeds": 0,
                            "skipped": 0,
                            "maximum_required_tokens": 12000,
                        }
                    ],
                }
            ],
            "summary": {"blocked_requests": 0, "unknown_only_requests": 0},
        }
    )

    rendered = output.getvalue()
    assert "Preflight baseline · benchmark · 0 generation calls" in rendered
    assert "Case sets used by this experiment" in rendered
    assert "Expanded fresh-run workload" in rendered
    assert "10 model calls" in rendered
    assert "Models and configured execution capacity" in rendered
    assert "Stage fit and usable concurrency" in rendered
    assert "solve" in rendered
    assert "local-a" in rendered
    assert "32768" in rendered
    assert "exact-fit route" in rendered


def test_preflight_json_flag_disables_human_output_in_a_tty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    human = _parser().parse_args(["preflight", "baseline"])
    machine = _parser().parse_args(["preflight", "baseline", "--json"])

    assert _human_preflight(human) is True
    assert _human_preflight(machine) is False


def test_terminal_reporter_shows_automatic_execution_error() -> None:
    output = StringIO()
    reporter = TerminalRunReporter(
        Console(file=output, force_terminal=False, color_system=None, width=120)
    )
    reporter.handle(
        ProgressEvent(
            "run_status",
            run_id="run-123",
            experiment_id="baseline",
            stage="validate_judge",
            details={
                "status": "execution_failed",
                "result": {
                    "error_kind": "connection_error",
                    "error": "judge DSPy call failed (connection_error): connection refused",
                },
            },
        )
    )
    reporter.close()

    rendered = output.getvalue()
    assert "Automatic execution failed at validate_judge" in rendered
    assert "connection refused" in rendered


def test_terminal_reporter_bounds_provider_error_payloads() -> None:
    output = StringIO()
    reporter = TerminalRunReporter(
        Console(file=output, force_terminal=False, color_system=None, width=120)
    )
    reporter.handle(
        ProgressEvent(
            "packet_failed",
            run_id="run-123",
            experiment_id="optimize-solver",
            stage="optimize",
            details={"error": "LM response: " + ("rewritten skill " * 500)},
        )
    )
    reporter.close()

    rendered = output.getvalue()
    assert "optimizer candidate set failed: LM response:" in rendered
    assert "..." in rendered
    assert len(rendered) < 400


def test_terminal_reporter_shows_parallel_judge_validation_votes() -> None:
    output = StringIO()
    reporter = TerminalRunReporter(
        Console(file=output, force_terminal=False, color_system=None, width=120)
    )
    common = {
        "run_id": "run-123",
        "experiment_id": "baseline",
        "stage": "validate_judge",
    }
    reporter.handle(
        ProgressEvent(
            "packet_started",
            **common,
            model="qwen3.6:27b-coding",
            details={"fixture_count": 5, "request_count": 15, "pool_capacity": 16},
        )
    )
    reporter.handle(
        ProgressEvent(
            "judge_validation_vote_completed",
            **common,
            position=1,
            total=15,
            duration_seconds=1.25,
            details={
                "fixture_id": "metatable-graph-strong",
                "vote": 2,
                "endpoint_id": "llama-server-10-11434",
            },
        )
    )
    reporter.close()

    rendered = output.getvalue()
    assert "5 fixtures · 15 votes · 16 slots" in rendered
    assert "[1/15] metatable-graph-strong · vote 2 · llama-server-10-11434" in rendered
