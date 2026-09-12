from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.errors import ConfigurationError, IntegrityError
from ms_agent_eval.core.evaluation import (
    CapabilityCatalog,
    CaseBank,
    CaseDefinition,
    deterministic_group_split,
    LlmJudge,
    SplitManifest,
    validate_case_authoring_provenance,
    validate_case_bank,
    validate_deterministic_splits,
)
from ms_agent_eval.core.hashing import sha256_file
from .helpers import create_workspace


def _retain_catalog_skills(repository, *skills: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    payload = yaml.safe_load(repository.capability_catalog_path.read_text(encoding="utf-8"))
    payload["skills"] = {skill: payload["skills"][skill] for skill in skills}
    repository.capability_catalog_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return payload


def test_case_bank_reports_per_skill_case_and_group_coverage(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    _retain_catalog_skills(repository, "alpha")

    create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    (repository.cases_root / "splits.yaml").write_text(
        "schema_version: 2\ngroups:\n  one-group: train\n", encoding="utf-8"
    )
    bank = CaseBank.discover(repository.cases_root)
    splits = SplitManifest.load(repository.split_file, bank)

    result = validate_case_bank(
        bank,
        capability_catalog=CapabilityCatalog.load(repository.capability_catalog_path),
        skill_ids=("alpha",),
        split_manifest=splits,
        source_paths=("skills/alpha/SKILL.md",),
        minimum_cases_per_skill=2,
        minimum_leakage_groups_per_skill=2,
    )

    assert result["status"] == "incomplete"
    assert result["coverage_gaps"] == [
        {
            "skill": "alpha",
            "cases": 1,
            "required_cases": 2,
            "leakage_groups": 1,
            "required_leakage_groups": 2,
        }
    ]


def test_capability_depth_is_a_separate_readiness_gate(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    catalog_payload = _retain_catalog_skills(repository, "alpha")
    catalog_payload["skills"]["alpha"]["capabilities"][0][
        "minimum_primary_cases"
    ] = 2
    repository.capability_catalog_path.write_text(
        yaml.safe_dump(catalog_payload, sort_keys=False), encoding="utf-8"
    )
    create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    (repository.cases_root / "splits.yaml").write_text(
        "schema_version: 2\ngroups:\n  one-group: train\n", encoding="utf-8"
    )
    bank = CaseBank.discover(repository.cases_root)
    result = validate_case_bank(
        bank,
        capability_catalog=CapabilityCatalog.load(repository.capability_catalog_path),
        skill_ids=("alpha",),
        split_manifest=SplitManifest.load(repository.split_file, bank),
        source_paths=("skills/alpha/SKILL.md",),
        minimum_cases_per_skill=1,
        minimum_leakage_groups_per_skill=1,
    )

    assert result["coverage_gaps"] == []
    assert result["capability_gaps"] == [
        {
            "skill": "alpha",
            "capability": "correctness",
            "primary_cases": 1,
            "required_primary_cases": 2,
            "missing_primary_cases": 1,
        }
    ]
    assert result["status"] == "incomplete"


def test_mainsequence_capability_catalog_uses_only_current_time_index_api() -> None:
    root = Path(__file__).resolve().parents[1]
    experiment = root / "experiments/mainsequence-sdk"
    catalog_path = experiment / "cases/coverage.yaml"
    text = catalog_path.read_text(encoding="utf-8")
    catalog = CapabilityCatalog.load(catalog_path)
    capability_ids = {
        item.id
        for item in (
            catalog.requirements_for("data_publishing/time_index_table_updates") or ()
        )
    }

    active_authoring_files = (
        experiment / "README.md",
        experiment / "case-authoring.md",
        *(
            path
            for path in (experiment / "cases").rglob("*")
            if path.is_file() and path.suffix in {".md", ".yaml"}
        ),
    )
    for path in active_authoring_files:
        contents = path.read_text(encoding="utf-8")
        assert "DataNode" not in contents
        assert "test_node" not in contents
    assert {
        "storage-contract",
        "updater-storage-separation",
        "update-configuration",
        "producer-and-reference-dependencies",
    } <= capability_ids
    assert "TimeIndexTableUpdater" in text
    assert "TimeIndexTableUpdateConfig" in text
    assert "TimeIndexTableUpdate" in text
    assert "TimeIndexTableRef" in text


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


def test_schema_v4_case_rejects_removed_evaluator_field(tmp_path: Path) -> None:
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


def test_case_authoring_provenance_is_checked_before_run_readiness(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    bank = CaseBank.discover(repository.cases_root)

    with pytest.raises(IntegrityError, match="active instructions"):
        validate_case_authoring_provenance(
            bank,
            authoring_instructions_hash=f"sha256:{'2' * 64}",
            capability_catalog_hash=sha256_file(repository.capability_catalog_path),
            source_snapshot_hash=f"sha256:{'3' * 64}",
        )

    with pytest.raises(IntegrityError, match="active authoring source set"):
        validate_case_authoring_provenance(
            bank,
            authoring_instructions_hash=f"sha256:{'1' * 64}",
            capability_catalog_hash=sha256_file(repository.capability_catalog_path),
            source_snapshot_hash=f"sha256:{'4' * 64}",
        )

    with pytest.raises(IntegrityError, match="active capability catalog"):
        validate_case_authoring_provenance(
            bank,
            authoring_instructions_hash=f"sha256:{'1' * 64}",
            capability_catalog_hash=f"sha256:{'4' * 64}",
            source_snapshot_hash=f"sha256:{'3' * 64}",
        )


def test_group_splits_are_reproducible_and_policy_checked(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    from .helpers import create_case

    case = create_case(
        repository.cases_root,
        case_id="one",
        skill="alpha",
        group="one-group",
        source_path="skills/alpha/SKILL.md",
    )
    expected = deterministic_group_split(
        case.group,
        seed="fixed-seed",
        train=0.5,
        development=0.2,
        test=0.2,
    )
    wrong = next(item for item in ("train", "development", "test", "challenge") if item != expected)
    split_file = repository.cases_root / "splits.yaml"
    split_file.write_text(
        f"schema_version: 2\ngroups:\n  {case.group}: {wrong}\n", encoding="utf-8"
    )
    manifest = SplitManifest.load(split_file, CaseBank.discover(repository.cases_root))

    with pytest.raises(IntegrityError, match="deterministic policy"):
        validate_deterministic_splits(
            manifest,
            seed="fixed-seed",
            train=0.5,
            development=0.2,
            test=0.2,
        )
