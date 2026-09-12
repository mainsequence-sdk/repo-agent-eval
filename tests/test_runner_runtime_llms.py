from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.errors import PreflightError
from ms_agent_eval.core.hashing import sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.runner import ExperimentRunner
from ms_agent_eval.core.workspace import ResolvedRoleModel
from ms_agent_eval.programs.dspy.budget import BudgetLedger

from .helpers import (
    FixedObservedLM,
    create_checkout,
    create_snapshot,
    create_workspace,
    judge_response,
    seed_cases_and_calibration,
    solver_response,
)


def _environment() -> Mapping[str, str]:
    return {"TEST_API_KEY": "test-secret"}


def _compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(repository, create_checkout(tmp_path), tmp_path / "external")
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    return compile_workspace(repository, snapshot=lock, snapshot_directory=directory)


def test_fixed_solver_and_judge_run_through_observed_dspy_contract(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    responses = {
        "solver": [solver_response() for _ in range(3)],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            *[judge_response(1.0) for _ in range(3)],
        ],
    }

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    run = ExperimentRunner(compiled, lm_factory=factory).run("baseline", environment=_environment())
    assert len(run.cases) == 3
    assert all(item.evaluation.passed for item in run.cases)
    assert run.role_usage["solver"]["model_calls"] == 3
    assert run.role_usage["judge"]["model_calls"] == 8
    assert set(run.role_usage) == {"solver", "judge"}
    assert all(call.role == "solver" for item in run.cases for call in item.solver.calls)


def test_optimization_publishes_json_state_before_held_out_test(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    responses = {
        "solver": [solver_response(), solver_response()],
        "judge": [
            judge_response(1.0),
            judge_response(0.5),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(0.0),
            judge_response(1.0),
            judge_response(1.0),
        ],
    }

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        assert api_key == "test-secret"
        del budget
        return FixedObservedLM(observer, responses[model.role], model=model.role)

    run = ExperimentRunner(compiled, lm_factory=factory).run("optimize", environment=_environment())
    assert run.compiled_program is not None
    assert [item.split for item in run.cases] == ["test"]
    manifest = (compiled.repository.data_root / run.compiled_program.relative_path).read_text(
        encoding="utf-8"
    )
    assert '"state_format":"json"' in manifest


def test_optimization_rejects_calibration_on_held_out_case_before_lm_creation(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    manifest = compiled.repository.calibration_root / "manifest.yaml"
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    for fixture in payload["fixtures"]:
        fixture["case"] = "case-test"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    recompiled = compile_workspace(
        compiled.repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    )
    calls = 0

    def factory(
        model: ResolvedRoleModel,
        observer: ModelCallObserver,
        budget: BudgetLedger | None,
        api_key: str | None,
    ) -> FixedObservedLM:
        nonlocal calls
        assert api_key == "test-secret"
        calls += 1
        return FixedObservedLM(observer, [], model=model.role)

    with pytest.raises(PreflightError, match="held-out"):
        ExperimentRunner(recompiled, lm_factory=factory).run("optimize", environment=_environment())
    assert calls == 0
