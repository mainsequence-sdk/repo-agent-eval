from __future__ import annotations

from io import StringIO

from rich.console import Console

from ms_agent_eval.core.cli import _human_progress, _parser
from ms_agent_eval.core.progress import ProgressEvent
from ms_agent_eval.ui import TerminalRunReporter


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
                "endpoint_id": "llama-server-10-11436",
            },
        )
    )
    reporter.close()

    rendered = output.getvalue()
    assert "5 fixtures · 15 votes · 16 slots" in rendered
    assert (
        "[1/15] metatable-graph-strong · vote 2 · llama-server-10-11436"
        in rendered
    )
