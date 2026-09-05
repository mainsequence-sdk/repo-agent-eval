from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.case_builder import (
    CaseBuilderService,
    CaseDraftStore,
    load_snapshot_context,
)
from ms_agent_eval.core.errors import ConfigurationError, IntegrityError
from ms_agent_eval.core.evaluation import CaseDefinition, LlmJudge
from ms_agent_eval.core.storage import FilesystemArtifactStore
from ms_agent_eval.core.workspace import ResolvedRoleModel
from ms_agent_eval.programs.dspy.engine import create_case_builder_program, program_hash

from .helpers import (
    FixedObservedLM,
    builder_response,
    create_checkout,
    create_snapshot,
    create_workspace,
)


def test_builder_writes_external_draft_and_only_promotion_writes_case_bank(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    checkout = create_checkout(tmp_path)
    data_root = tmp_path / "external"
    lock, directory = create_snapshot(repository, checkout, data_root)
    context = load_snapshot_context(lock, directory)
    artifacts = FilesystemArtifactStore(data_root, workspace_root=repository.root)
    drafts = CaseDraftStore(data_root, workspace_root=repository.root, artifacts=artifacts)
    service = CaseBuilderService(repository, context, artifacts, drafts)
    model = ResolvedRoleModel.resolve(
        "case_builder",
        repository.workspace.evaluation.case_builder.model,
        {},
    )
    draft = service.build(
        skill="alpha",
        coverage_request="Create one grounded case",
        model=model,
        lm_factory=lambda observer: FixedObservedLM(
            observer, [builder_response()], model="builder"
        ),
    )
    assert draft.status == "validated"
    assert not tuple(repository.cases_root.rglob("case.yaml"))
    promoted = drafts.promote(draft.id, repository.cases_root)
    assert promoted.id == "built-case"
    assert promoted.provenance.builder_model_hash == model.content_hash
    assert promoted.provenance.builder_program_hash == program_hash(
        create_case_builder_program(repository.case_authoring_instructions())
    )
    assert len(draft.call_ids) == 1


def test_llm_judge_validates_exact_criterion_ids(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    case = create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    judge = LlmJudge(
        lambda **_: {
            "criterion_scores": {"wrong": 1.0},
            "hard_failures": [],
            "feedback": "feedback",
        },
        model_hash="judge-model",
        program_hash="judge-program",
        repetitions=1,
    )
    with pytest.raises(IntegrityError, match="match rubric exactly"):
        judge.evaluate(case, "answer", skill_context="skill")


def test_llm_judge_uses_median_votes_and_weighted_arithmetic(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    case: CaseDefinition = create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    values = iter((0.2, 1.0, 0.8))
    judge = LlmJudge(
        lambda **_: {
            "criterion_scores": {"correctness": next(values)},
            "hard_failures": [],
            "feedback": "feedback",
        },
        model_hash="judge-model",
        program_hash="judge-program",
        repetitions=3,
    )
    result = judge.evaluate(case, "answer", skill_context="skill")
    assert result.score == 0.8
    assert result.passed is True


def test_schema_v2_case_rejects_removed_evaluator_field(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    case = create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    case_file = case.path / "case.yaml"
    payload = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    payload["evaluator"] = {"method": "removed-semantic-method"}
    case_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown fields"):
        CaseDefinition.load(case.path)
