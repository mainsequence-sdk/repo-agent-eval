from __future__ import annotations

import json
import sys
from pathlib import Path

from ms_agent_eval.core.cli import _parser, main
from ms_agent_eval.core import planning
from ms_agent_eval.core.planning import inspect_workspace as inspect_workspace_real
from ms_agent_eval.core.workspace import WorkspaceRepository


def test_init_creates_one_manifest_and_external_default(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace.yaml"
    result = main(
        [
            "init",
            "--id",
            "example-evaluation",
            "--repo",
            "https://github.com/example/repository",
            "--ref",
            "v1.0.0",
            "--global-instructions",
            "AGENTS.md",
            "--skills-directory",
            "skills",
            "--cases",
            "cases",
            "--api-base-url",
            "http://localhost:8000/v1",
            "--api-key-env",
            "OPENAI_API_KEY",
            "--solver-model",
            "solver",
            "--solver-reasoning-effort",
            "high",
            "--judge-model",
            "judge",
            "--judge-reasoning-effort",
            "none",
            "--workspace",
            str(workspace),
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_root"].endswith("ms_agent_eval/example-evaluation")
    repository = WorkspaceRepository.from_file(workspace)
    assert repository.workspace.schema_version == 9
    assert repository.workspace.models["solver"].reasoning_effort == "high"
    assert repository.workspace.models["judge"].reasoning_effort == "none"
    assert set(repository.workspace.executors) == {
        "remote-models",
        "authoring-harness",
        "local-process",
    }
    assert repository.workspace.stages["author"].model == "author"
    assert repository.case_authoring_path == tmp_path / "case-authoring.md"
    assert "Evaluation Case Authoring Contract" in repository.case_authoring_instructions()
    assert "`error_identification`" in repository.case_authoring_instructions()
    assert "Mandatory judge-calibration deliverable" in (
        repository.case_authoring_instructions()
    )
    assert "Author, solver, and judge responsibilities" in (
        repository.case_authoring_instructions()
    )
    assert "If any calibration condition fails" in (
        repository.case_authoring_instructions()
    )
    assert "Do not stop after writing cases" in repository.case_authoring_instructions()
    for label in ("strong", "partial", "incorrect", "contradictory", "adversarial"):
        assert f"`{label}`" in repository.case_authoring_instructions()
    case_bank_readme = (tmp_path / "cases" / "README.md").read_text(encoding="utf-8")
    assert "Case Bank and Coverage Contract" in case_bank_readme
    assert "Only primary cases count" in case_bank_readme
    assert "sibling `judge-calibration/`" in case_bank_readme
    assert payload["case_bank_readme"].endswith("cases/README.md")
    assert "Mandatory semantic coverage contract" in (
        tmp_path / "cases" / "coverage.yaml"
    ).read_text(encoding="utf-8")
    assert set(repository.workspace.experiments) == {"baseline", "optimize-few-shot"}
    assert repository.workspace.evaluation.case_authoring.instructions == "case-authoring.md"
    assert repository.workspace.evaluation.cases.coverage.minimum_cases_per_skill == 8
    assert (
        repository.workspace.evaluation.cases.coverage.minimum_leakage_groups_per_skill
        == 4
    )
    assert (tmp_path / ".env.example").read_text(encoding="utf-8").endswith(
        "OPENAI_API_KEY=\n"
    )
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".env\n"


def test_no_arguments_in_noninteractive_terminal_prints_help(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main([]) == 2

    output = capsys.readouterr()
    assert "ms-agent-eval" in output.err
    assert "setup" in output.err


def test_cli_has_no_backend_case_builder_command() -> None:
    assert "cases" not in _parser().format_help()


def test_cli_exposes_deterministic_report_backfill() -> None:
    arguments = _parser().parse_args(
        ["report", "build", "--run", "run-1", "--workspace", "workspace.yaml"]
    )

    assert arguments.command == "report"
    assert arguments.report_command == "build"
    assert arguments.run == "run-1"
    assert arguments.all is False


def test_validate_serializes_bootstrap_report(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from .helpers import create_checkout, create_snapshot, create_workspace

    repository = create_workspace(tmp_path / "workspace")
    snapshot, snapshot_directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    (repository.cases_root / "splits.yaml").write_text(
        "schema_version: 2\ngroups: {}\n", encoding="utf-8"
    )
    (repository.calibration_root / "manifest.yaml").write_text(
        "schema_version: 2\nfixtures: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        planning,
        "inspect_workspace",
        lambda repository, environment: inspect_workspace_real(
            repository,
            environment={},
            snapshot=snapshot,
            snapshot_directory=snapshot_directory,
        ),
    )

    result = main(["validate", "--workspace", str(repository.workspace_file)])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "incomplete"
    assert payload["case_authoring"]["stage"]["executor"] == "authoring-harness"
    assert payload["case_authoring"]["instructions"].endswith("case-authoring.md")
