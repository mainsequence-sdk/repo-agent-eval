"""Current workspace configuration and readiness tests."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.authoring import load_snapshot_context
from ms_agent_eval.core.coordinator import (
    _rehydrate_historical_experiment_lock,
    _upgrade_frozen_workspace_payload,
)
from ms_agent_eval.core.errors import ConfigurationError, IntegrityError, ResolutionError
from ms_agent_eval.core.hashing import json_value, sha256_file
from ms_agent_eval.core.workspace import (
    WorkspaceConfiguration,
    WorkspaceRepository,
    resolve_role_models,
)
from ms_agent_eval.core import planning
from ms_agent_eval.core.planning import acquire_snapshot, compile_workspace, inspect_workspace
from ms_agent_eval.core.snapshots import ExternalSnapshotStore, SnapshotBuilder
from ms_agent_eval.core.sources import ResolvedSource

from .helpers import (
    create_checkout,
    create_snapshot,
    create_workspace,
    seed_cases_and_judge_validation,
    workspace_payload,
)


def _compiled_workspace_with_cases(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    seed_cases_and_judge_validation(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    return compile_workspace(
        repository,
        snapshot=lock,
        snapshot_directory=directory,
    )


def test_schema_v12_rejects_case_authoring_model_configuration() -> None:
    payload = workspace_payload()
    payload["evaluation"]["case_authoring"]["model"] = {"name": "builder"}  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)


def test_schema_v13_is_upgraded_only_for_an_immutable_frozen_run() -> None:
    legacy = workspace_payload()
    legacy["schema_version"] = 13
    deployments = legacy.pop("deployments")
    legacy.pop("recording")
    legacy.pop("recorders")
    legacy.pop("comparisons", None)
    models = legacy["models"]
    assert isinstance(models, dict)
    for model_id, raw_model in models.items():
        assert isinstance(raw_model, dict)
        deployment = deployments[raw_model.pop("deployment")]
        raw_model["name"] = deployment["model"]
        raw_model["deployment_revision"] = deployment["revision"]
    stages = legacy["stages"]
    assert isinstance(stages, dict)
    for stage_id, model_id in {
        "validate_judge": "judge",
        "optimize": "optimizer",
        "solve": "solver",
        "judge": "judge",
    }.items():
        stages[stage_id]["executor"] = "remote-models"
        stages[stage_id]["model"] = model_id
    executors = legacy["executors"]
    assert isinstance(executors, dict)
    for endpoint in executors["remote-models"]["endpoints"]:
        endpoint.pop("routes")
    experiments = legacy["experiments"]
    assert isinstance(experiments, dict)
    for experiment in experiments.values():
        experiment.pop("roles")
        if experiment.get("mode") == "optimize":
            validation = experiment.pop("validation")
            experiment["cross_validation"] = {
                "folds": 2,
                "seed": validation["seed"],
            }

    with pytest.raises(ConfigurationError, match="schema_version must be 17"):
        WorkspaceConfiguration.from_mapping(legacy)

    upgraded = _upgrade_frozen_workspace_payload(legacy)
    current = workspace_payload()
    upgraded["recording"] = current["recording"]
    upgraded["recorders"] = current["recorders"]
    parsed = WorkspaceConfiguration.from_mapping(upgraded)

    assert parsed.schema_version == 17
    assert parsed.experiments["baseline"].roles.solver == "solver"
    assert parsed.models["solver"].deployment == "frozen-solver"
    assert {
        route.deployment for route in parsed.executors["remote-models"].endpoints[0].routes
    } == {"frozen-judge", "frozen-optimizer", "frozen-solver"}


def test_schema_v14_is_upgraded_only_for_an_immutable_frozen_run() -> None:
    frozen = workspace_payload()
    frozen["schema_version"] = 14
    experiments = frozen["experiments"]
    assert isinstance(experiments, dict)
    optimize = experiments["optimize"]
    optimize["roles"]["optimizer"] = "optimizer"
    optimize["optimizer"] = {
        "strategy": "copro",
        "targets": {"prompt": True, "skills": "all"},
        "search": {"breadth": 3, "depth": 2, "init_temperature": 1.4},
        "metric": {"name": "rubric_mean_score"},
    }
    optimize["budget"]["optimizer"] = {"model_calls": 20, "tokens": 10000}
    validation = optimize.pop("validation")
    optimize["cross_validation"] = {
        "folds": 2,
        "seed": validation["seed"],
    }

    with pytest.raises(ConfigurationError, match="schema_version must be 17"):
        WorkspaceConfiguration.from_mapping(frozen)

    upgraded = _upgrade_frozen_workspace_payload(frozen)
    parsed = WorkspaceConfiguration.from_mapping(upgraded)

    assert parsed.schema_version == 17
    assert parsed.experiments["optimize"].validation is not None
    assert parsed.experiments["optimize"].validation.seed == 0
    optimizer = parsed.experiments["optimize"].optimizer
    assert optimizer is not None
    assert optimizer.search_space_type == "skills"  # type: ignore[union-attr]


def test_schema_v7_lock_rehydration_preserves_historical_model_and_run_identity(
    tmp_path: Path,
) -> None:
    compiled = _compiled_workspace_with_cases(tmp_path)
    generated = compiled.experiment_lock("optimize")
    stored = dict(json_value(generated))
    stored["schema_version"] = 7
    revision = "sha256:" + "a" * 64
    stored["content_hash"] = revision
    configuration = dict(stored["configuration"])
    configuration.pop("roles")
    stored["configuration"] = configuration
    historical_hashes = {}
    raw_models = {}
    for index, (role, raw_model) in enumerate(stored["models"].items(), start=1):
        model = dict(raw_model)
        model.pop("profile_id")
        model.pop("deployment_id")
        model["content_hash"] = "sha256:" + str(index) * 64
        historical_hashes[role] = model["content_hash"]
        raw_models[role] = model
    stored["models"] = raw_models

    historical = _rehydrate_historical_experiment_lock(
        stored,
        generated,
        expected_revision=revision,
        judge_repetitions=compiled.repository.workspace.evaluation.judge.repetitions,
    )

    assert historical.schema_version == 7
    assert historical.content_hash == revision
    assert {
        role: model.content_hash for role, model in historical.models.items()
    } == historical_hashes


def test_recorder_configuration_rejects_secrets_and_unsafe_templates() -> None:
    payload = workspace_payload()
    payload["recorders"]["local-mlflow"]["authentication"] = {
        "type": "basic",
        "username_env": "MLFLOW_USER",
        "password": "must-not-be-versioned",
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)

    payload = workspace_payload()
    payload["recorders"]["local-mlflow"]["experiment_name"] = "{workspace_id}/{unsupported}"
    with pytest.raises(ConfigurationError, match="supports only"):
        WorkspaceConfiguration.from_mapping(payload)

    payload = workspace_payload()
    payload["recorders"]["local-mlflow"]["tracking_uri"] = "file:///tmp/mlruns"
    with pytest.raises(ConfigurationError, match="HTTP"):
        WorkspaceConfiguration.from_mapping(payload)


def test_endpoint_pool_requires_explicit_structured_capacity() -> None:
    payload = workspace_payload()
    payload["executors"]["remote-models"] = {  # type: ignore[index]
        "type": "endpoint",
        "adapter": "openai_compatible",
        "base_url": "http://localhost:8000/v1",
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)

    payload = workspace_payload()
    del payload["executors"]["remote-models"]["endpoints"][0][  # type: ignore[index]
        "max_concurrent_requests"
    ]
    with pytest.raises(ConfigurationError, match="positive integer"):
        WorkspaceConfiguration.from_mapping(payload)


def test_endpoint_pool_rejects_duplicate_ids_and_urls() -> None:
    for changed_field in ("id", "base_url"):
        payload = workspace_payload()
        endpoints = payload["executors"]["remote-models"]["endpoints"]  # type: ignore[index]
        duplicate = dict(endpoints[0])
        duplicate["id"] = "secondary"
        duplicate["base_url"] = "http://localhost:8001/v1"
        duplicate[changed_field] = endpoints[0][changed_field]
        endpoints.append(duplicate)
        with pytest.raises(ConfigurationError, match="must be unique"):
            WorkspaceConfiguration.from_mapping(payload)


def test_endpoint_topology_is_attempt_identity_not_semantic_model_identity() -> None:
    original = WorkspaceConfiguration.from_mapping(workspace_payload())
    changed_payload = workspace_payload()
    endpoints = changed_payload["executors"]["remote-models"]["endpoints"]  # type: ignore[index]
    endpoints[0]["max_concurrent_requests"] = 8
    endpoints.append(
        {
            "id": "secondary",
            "base_url": "http://localhost:8001/v1",
            "max_concurrent_requests": 4,
            "routes": list(endpoints[0]["routes"]),
        }
    )
    changed = WorkspaceConfiguration.from_mapping(changed_payload)
    original_executor = original.executors["remote-models"]
    changed_executor = changed.executors["remote-models"]

    assert original_executor.execution_hash == changed_executor.execution_hash
    assert original_executor.pool_hash != changed_executor.pool_hash
    assert original.identity_hash != changed.identity_hash
    assert (
        resolve_role_models(original, "baseline")["solver"].content_hash
        == resolve_role_models(changed, "baseline")["solver"].content_hash
    )


def test_model_uses_only_endpoints_that_route_its_deployment() -> None:
    payload = workspace_payload()
    endpoints = payload["executors"]["remote-models"]["endpoints"]  # type: ignore[index]
    endpoints.append(
        {
            "id": "glm-only",
            "base_url": "http://localhost:8001/v1",
            "max_concurrent_requests": 7,
            "routes": [{"deployment": "glm-test", "model": "glm-coding"}],
        }
    )
    payload["deployments"]["glm-test"] = {  # type: ignore[index]
        "executor": "remote-models",
        "model": "glm-coding",
        "revision": "glm-test-v1",
    }
    workspace = WorkspaceConfiguration.from_mapping(payload)
    executor = workspace.executors["remote-models"]
    solver = resolve_role_models(workspace, "baseline")["solver"]

    assert [endpoint.id for endpoint in solver.eligible_endpoints(executor)] == ["primary"]
    assert solver.bind_endpoint(executor.endpoints[0]).model == "solver"
    with pytest.raises(ResolutionError, match="does not serve deployment"):
        solver.bind_endpoint(executor.endpoints[1])


def test_endpoint_route_requires_a_known_deployment_on_the_same_executor() -> None:
    unknown = workspace_payload()
    unknown["executors"]["remote-models"]["endpoints"][0]["routes"] = [  # type: ignore[index]
        {"deployment": "missing", "model": "solver"}
    ]
    with pytest.raises(ConfigurationError, match="unknown deployment"):
        WorkspaceConfiguration.from_mapping(unknown)

    wrong_executor = workspace_payload()
    wrong_executor["executors"]["remote-models"]["endpoints"][0]["routes"] = [  # type: ignore[index]
        {"deployment": "author-test", "model": "author"}
    ]
    with pytest.raises(ConfigurationError, match="belongs to executor"):
        WorkspaceConfiguration.from_mapping(wrong_executor)


def test_experiment_can_belong_to_only_one_named_comparison() -> None:
    payload = workspace_payload()
    payload["comparisons"] = {
        "first": {
            "baseline_experiments": ["baseline"],
            "optimization_experiments": ["optimize"],
        },
        "second": {
            "baseline_experiments": ["baseline"],
            "optimization_experiments": ["optimize"],
        },
    }

    with pytest.raises(ConfigurationError, match="only one comparison"):
        WorkspaceConfiguration.from_mapping(payload)


def test_comparison_requires_supported_metric_unique_grouping_and_its_own_baseline() -> None:
    payload = workspace_payload()
    payload["comparisons"] = {
        "model-arms": {
            "baseline_experiments": ["baseline"],
            "optimization_experiments": ["optimize"],
            "primary_metric": "score.median",
            "grouping": ["solver", "optimizer"],
        }
    }
    with pytest.raises(ConfigurationError, match="supports only score.mean"):
        WorkspaceConfiguration.from_mapping(payload)

    payload["comparisons"]["model-arms"]["primary_metric"] = "score.mean"
    payload["comparisons"]["model-arms"]["grouping"] = ["solver", "solver"]
    with pytest.raises(ConfigurationError, match="grouping values must be unique"):
        WorkspaceConfiguration.from_mapping(payload)

    payload["comparisons"]["model-arms"]["grouping"] = ["solver", "optimizer"]
    payload["experiments"]["second-baseline"] = dict(payload["experiments"]["baseline"])
    payload["comparisons"]["model-arms"]["baseline_experiments"] = ["second-baseline"]
    with pytest.raises(ConfigurationError, match="not one of its baseline experiments"):
        WorkspaceConfiguration.from_mapping(payload)


def test_comparison_requires_one_fixed_judge_profile() -> None:
    payload = workspace_payload()
    payload["models"]["other-judge"] = dict(payload["models"]["judge"])
    payload["experiments"]["optimize"]["roles"]["judge"] = "other-judge"
    payload["experiments"]["baseline"]["roles"]["judge"] = "other-judge"
    payload["experiments"]["second-baseline"] = dict(payload["experiments"]["baseline"])
    payload["experiments"]["second-baseline"]["roles"] = {
        "solver": "solver",
        "judge": "judge",
    }
    payload["comparisons"] = {
        "model-arms": {
            "baseline_experiments": ["baseline", "second-baseline"],
            "optimization_experiments": ["optimize"],
            "primary_metric": "score.mean",
            "grouping": ["solver", "optimizer"],
        }
    }

    with pytest.raises(ConfigurationError, match="one fixed judge profile"):
        WorkspaceConfiguration.from_mapping(payload)


def test_skills_require_exactly_one_selection_form() -> None:
    both = workspace_payload(skills={"directory": "skills", "files": ["skills/a/SKILL.md"]})
    neither = workspace_payload(skills={})
    with pytest.raises(ConfigurationError, match="exactly one"):
        WorkspaceConfiguration.from_mapping(both)
    with pytest.raises(ConfigurationError, match="exactly one"):
        WorkspaceConfiguration.from_mapping(neither)


def test_explicit_skill_files_are_the_only_directory_alternative() -> None:
    workspace = WorkspaceConfiguration.from_mapping(
        workspace_payload(
            skills={
                "files": [
                    "skills/alpha/SKILL.md",
                    "skills/beta/SKILL.md",
                ]
            }
        )
    )
    assert workspace.evaluation.instructions.skills.directory is None
    assert workspace.evaluation.instructions.skills.files == (
        "skills/alpha/SKILL.md",
        "skills/beta/SKILL.md",
    )


def test_hash_verified_workspace_skill_import_is_namespaced_and_addressed(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    imported_file = repository.root / "instructions/platform/skills/agents/a2a/SKILL.md"
    imported_file.parent.mkdir(parents=True)
    imported_file.write_text("# Platform A2A\n\nUse the locked protocol.\n", encoding="utf-8")
    manifest = repository.root / "instructions/platform/manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "id": "platform",
                "source": {
                    "type": "mcp",
                    "manifest_hash": f"sha256:{'a' * 64}",
                    "ontology_hash": f"sha256:{'b' * 64}",
                },
                "skills": [
                    {
                        "id": "agents/a2a",
                        "name": "a2a_communication",
                        "uri": "example://skills/a2a",
                        "resource_path": "skills/agents/a2a/SKILL.md",
                        "file": "instructions/platform/skills/agents/a2a/SKILL.md",
                        "content_hash": sha256_file(imported_file),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    workspace_document = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    workspace_document["evaluation"]["instructions"]["imports"] = [
        {
            "id": "platform",
            "source": "mcp",
            "note": "Platform-owned skills captured from MCP.",
            "manifest": "instructions/platform/manifest.yaml",
            "skills": {"directory": "instructions/platform/skills"},
        }
    ]
    repository.workspace_file.write_text(
        yaml.safe_dump(workspace_document, sort_keys=False), encoding="utf-8"
    )
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )

    context = load_snapshot_context(
        lock,
        directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
        workspace_root=repository.root,
        instruction_imports=repository.workspace.evaluation.instructions.imports,
    )

    assert context.skill("platform/agents/a2a") == imported_file.read_text(encoding="utf-8")
    assert context.snapshot_hash != lock.content_hash
    assert context.workspace_source_paths == (
        "instructions/platform/manifest.yaml",
        "instructions/platform/skills/agents/a2a/SKILL.md",
    )
    assert context.instruction_imports[0].note == "Platform-owned skills captured from MCP."
    assert context.instruction_units[-1].source_type == "mcp"


def test_workspace_skill_import_rejects_content_drift(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    imported_file = repository.root / "instructions/platform/skills/a/SKILL.md"
    imported_file.parent.mkdir(parents=True)
    imported_file.write_text("# Changed\n", encoding="utf-8")
    manifest = repository.root / "instructions/platform/manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "id": "platform",
                "source": {"type": "mcp"},
                "skills": [
                    {
                        "id": "a",
                        "name": "a",
                        "uri": "example://skills/a",
                        "resource_path": "skills/a/SKILL.md",
                        "file": "instructions/platform/skills/a/SKILL.md",
                        "content_hash": f"sha256:{'0' * 64}",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["evaluation"]["instructions"]["imports"] = [
        {
            "id": "platform",
            "source": "mcp",
            "note": "Locked external skills.",
            "manifest": "instructions/platform/manifest.yaml",
            "skills": {"directory": "instructions/platform/skills"},
        }
    ]
    repository.workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(repository.workspace_file)
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )

    with pytest.raises(IntegrityError, match="content hash mismatch"):
        load_snapshot_context(
            lock,
            directory,
            global_paths=repository.workspace.evaluation.instructions.global_paths,
            workspace_root=repository.root,
            instruction_imports=repository.workspace.evaluation.instructions.imports,
        )


def test_case_coverage_requires_multiple_cases_and_semantic_groups_per_skill() -> None:
    payload = workspace_payload()
    payload["evaluation"]["cases"]["coverage"] = {  # type: ignore[index]
        "catalog": "cases/coverage.yaml",
        "minimum_cases_per_skill": 12,
        "minimum_leakage_groups_per_skill": 6,
    }

    workspace = WorkspaceConfiguration.from_mapping(payload)

    assert workspace.evaluation.cases.coverage.minimum_cases_per_skill == 12
    assert workspace.evaluation.cases.coverage.minimum_leakage_groups_per_skill == 6

    payload["evaluation"]["cases"]["coverage"] = {  # type: ignore[index]
        "catalog": "cases/coverage.yaml",
        "minimum_cases_per_skill": 4,
        "minimum_leakage_groups_per_skill": 5,
    }
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        WorkspaceConfiguration.from_mapping(payload)


def test_case_coverage_and_catalog_are_required() -> None:
    payload = workspace_payload()
    del payload["evaluation"]["cases"]["coverage"]  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="evaluation.cases.coverage"):
        WorkspaceConfiguration.from_mapping(payload)

    payload = workspace_payload()
    del payload["evaluation"]["cases"]["coverage"]["catalog"]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="coverage.catalog"):
        WorkspaceConfiguration.from_mapping(payload)


def test_configured_llm_roles_may_share_a_model_but_keep_distinct_stage_roles() -> None:
    workspace = WorkspaceConfiguration.from_mapping(workspace_payload())
    models = resolve_role_models(
        workspace,
        "baseline",
        {"UNRELATED_PROCESS_SETTING": "cannot-override-workspace-models"},
    )
    assert set(models) == {"solver", "judge"}
    assert {role: model.model for role, model in models.items()} == {
        "solver": "solver",
        "judge": "judge",
    }
    payload = workspace_payload()
    payload["models"]["judge"]["deployment"] = "solver-test"  # type: ignore[index]
    workspace = WorkspaceConfiguration.from_mapping(payload)
    shared = resolve_role_models(workspace, "baseline")
    assert shared["solver"].model == shared["judge"].model == "solver"


def test_reasoning_effort_is_a_first_class_hashed_model_field() -> None:
    payload = workspace_payload()
    payload["models"]["solver"]["reasoning_effort"] = "high"  # type: ignore[index]

    workspace = WorkspaceConfiguration.from_mapping(payload)
    resolved = resolve_role_models(workspace, "baseline")["solver"]

    assert workspace.models["solver"].reasoning_effort == "high"
    assert resolved.reasoning_effort == "high"
    assert "reasoning_effort" not in resolved.parameters

    default_hash = resolve_role_models(
        WorkspaceConfiguration.from_mapping(workspace_payload()), "baseline"
    )["solver"].content_hash
    assert resolved.content_hash != default_hash


def test_deployment_revision_is_required_and_invalidates_model_identity() -> None:
    missing = workspace_payload()
    del missing["deployments"]["solver-test"]["revision"]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="revision"):
        WorkspaceConfiguration.from_mapping(missing)

    changed = workspace_payload()
    changed["deployments"]["solver-test"]["revision"] = "solver-test-v2"  # type: ignore[index]
    original_hash = resolve_role_models(
        WorkspaceConfiguration.from_mapping(workspace_payload()), "baseline"
    )["solver"].content_hash
    changed_hash = resolve_role_models(WorkspaceConfiguration.from_mapping(changed), "baseline")[
        "solver"
    ].content_hash
    assert changed_hash != original_hash


def test_stage_reuse_policy_is_explicit_and_replicate_is_solver_only() -> None:
    payload = workspace_payload()
    del payload["stages"]["solve"]["reuse"]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="stages.solve.reuse"):
        WorkspaceConfiguration.from_mapping(payload)

    payload = workspace_payload()
    payload["stages"]["solve"]["reuse"] = {"policy": "replicate", "samples": 3}  # type: ignore[index]
    workspace = WorkspaceConfiguration.from_mapping(payload)
    assert workspace.stages["solve"].reuse.samples == 3

    payload = workspace_payload()
    payload["stages"]["judge"]["reuse"] = {"policy": "replicate", "samples": 2}  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="supported only for solve"):
        WorkspaceConfiguration.from_mapping(payload)


def test_unrelated_experiment_change_does_not_change_baseline_revision(
    tmp_path: Path,
) -> None:
    compiled = _compiled_workspace_with_cases(tmp_path)
    original = compiled.experiment_lock("baseline")
    workspace_file = compiled.repository.workspace_file
    payload = yaml.safe_load(workspace_file.read_text(encoding="utf-8"))
    payload["experiments"]["optimize"]["validation"]["seed"] = 99
    workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(workspace_file)
    changed = compile_workspace(
        repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    ).experiment_lock("baseline")

    assert changed.workspace_hash != original.workspace_hash
    assert changed.content_hash == original.content_hash


def test_comparison_membership_does_not_invalidate_experiment_work(
    tmp_path: Path,
) -> None:
    compiled = _compiled_workspace_with_cases(tmp_path)
    original = compiled.experiment_lock("baseline")
    workspace_file = compiled.repository.workspace_file
    payload = yaml.safe_load(workspace_file.read_text(encoding="utf-8"))
    payload["comparisons"] = {
        "model-arms": {
            "baseline_experiments": ["baseline"],
            "optimization_experiments": ["optimize"],
            "primary_metric": "score.mean",
            "grouping": ["solver", "optimizer"],
        }
    }
    workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(workspace_file)
    changed = compile_workspace(
        repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    ).experiment_lock("baseline")

    assert original.comparison_id == "all-compatible-arms"
    assert changed.comparison_id == "model-arms"
    assert changed.content_hash == original.content_hash
    assert changed.arm_id == original.arm_id


@pytest.mark.parametrize("effort", ("off", "extreme", 3))
def test_reasoning_effort_rejects_invalid_values(effort: object) -> None:
    payload = workspace_payload()
    payload["models"]["solver"]["reasoning_effort"] = effort  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="reasoning_effort"):
        WorkspaceConfiguration.from_mapping(payload)


def test_reasoning_effort_is_rejected_inside_generic_parameters() -> None:
    payload = workspace_payload()
    payload["models"]["solver"]["parameters"]["reasoning_effort"] = "high"  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="first-class model field"):
        WorkspaceConfiguration.from_mapping(payload)


def test_configured_api_key_reference_is_required_only_at_request_time() -> None:
    payload = workspace_payload()
    executor = payload["executors"]["remote-models"]  # type: ignore[index]
    executor["endpoints"][0]["api_key_env"] = "OPENAI_API_KEY"  # type: ignore[index]
    workspace = WorkspaceConfiguration.from_mapping(payload)
    endpoint = workspace.executors["remote-models"].endpoints[0]

    with pytest.raises(ResolutionError, match="endpoint 'primary' requires"):
        endpoint.api_key({})
    assert endpoint.api_key({"OPENAI_API_KEY": "secret"}) == "secret"


def test_judge_validation_resolves_only_from_the_experiment_judge_role() -> None:
    payload = workspace_payload()
    payload["stages"]["validate_judge"]["model"] = "solver"  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="experiment.roles"):
        WorkspaceConfiguration.from_mapping(payload)


def test_optimizer_is_a_strict_discriminated_strategy_union() -> None:
    labeled = WorkspaceConfiguration.from_mapping(workspace_payload()).experiments["optimize"]
    assert labeled.optimizer is not None
    assert labeled.optimizer.strategy == "labeled_few_shot"
    assert labeled.optimizer.k_values == (1,)

    legacy = workspace_payload()
    legacy["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "name": "LabeledFewShot",
        "search": {"k": [1]},
    }
    with pytest.raises(ConfigurationError, match="optimizer.name was removed"):
        WorkspaceConfiguration.from_mapping(legacy)

    mixed = workspace_payload()
    mixed["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "search_space": {"type": "skills", "include": ["alpha"]},
        "search": {"breadth": 2, "depth": 1, "init_temperature": 1.0, "k": [1]},
        "metric": {"name": "rubric_mean_score"},
    }
    mixed["experiments"]["optimize"]["budget"]["optimizer"] = {  # type: ignore[index]
        "model_calls": 10,
        "tokens": 10000,
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(mixed)

    removed_targets = workspace_payload()
    removed_targets["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "targets": {"prompt": True, "skills": "all"},
        "search": {"breadth": 2, "depth": 1, "init_temperature": 1.0},
        "metric": {"name": "rubric_mean_score"},
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(removed_targets)

    removed_partitions = workspace_payload()
    removed_partitions["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "search_space": {"type": "skills", "include": ["alpha"]},
        "search": {
            "breadth": 2,
            "depth": 1,
            "partitions": 2,
            "init_temperature": 1.0,
        },
        "metric": {"name": "rubric_mean_score"},
    }
    removed_partitions["experiments"]["optimize"]["budget"]["optimizer"] = {  # type: ignore[index]
        "model_calls": 10,
        "tokens": 10000,
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(removed_partitions)

    mixed_space = workspace_payload()
    mixed_space["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "search_space": {"type": "skills", "include": ["alpha"], "files": "all"},
        "search": {"breadth": 2, "depth": 1, "init_temperature": 1.0},
        "metric": {"name": "rubric_mean_score"},
    }
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(mixed_space)


def test_copro_global_instruction_space_accepts_only_configured_exact_paths() -> None:
    payload = workspace_payload()
    payload["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "search_space": {"type": "global_instructions", "files": ["AGENTS.md"]},
        "search": {"breadth": 2, "depth": 1, "init_temperature": 1.0},
        "metric": {"name": "rubric_mean_score"},
    }
    payload["experiments"]["optimize"]["roles"]["optimizer"] = "optimizer"  # type: ignore[index]
    payload["experiments"]["optimize"]["budget"]["optimizer"] = {  # type: ignore[index]
        "model_calls": 10,
        "tokens": 10000,
    }

    workspace = WorkspaceConfiguration.from_mapping(payload)
    optimizer = workspace.experiments["optimize"].optimizer
    assert optimizer is not None
    assert optimizer.search_space_type == "global_instructions"  # type: ignore[union-attr]
    assert optimizer.global_instruction_files == ("AGENTS.md",)  # type: ignore[union-attr]
    assert optimizer.skills == ()  # type: ignore[union-attr]

    payload["experiments"]["optimize"]["optimizer"]["search_space"]["files"] = [  # type: ignore[index]
        "missing/AGENTS.md"
    ]
    with pytest.raises(ConfigurationError, match="not declared"):
        WorkspaceConfiguration.from_mapping(payload)


def test_copro_resolves_a_distinct_optimizer_role_and_requires_its_budget() -> None:
    payload = workspace_payload()
    payload["experiments"]["optimize"]["optimizer"] = {  # type: ignore[index]
        "strategy": "copro",
        "search_space": {"type": "skills", "include": ["alpha"]},
        "search": {"breadth": 2, "depth": 1, "init_temperature": 1.0},
        "metric": {"name": "rubric_mean_score"},
    }
    payload["experiments"]["optimize"]["roles"]["optimizer"] = "optimizer"  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="requires budget.optimizer"):
        WorkspaceConfiguration.from_mapping(payload)

    payload["experiments"]["optimize"]["budget"]["optimizer"] = {  # type: ignore[index]
        "model_calls": 10,
        "tokens": 10000,
    }
    workspace = WorkspaceConfiguration.from_mapping(payload)
    models = resolve_role_models(workspace, "optimize")
    assert set(models) == {"optimizer", "solver", "judge"}
    assert models["optimizer"].model == "optimizer"


def test_noncanonical_solver_signature_is_rejected() -> None:
    payload = workspace_payload()
    payload["experiments"]["baseline"]["solver"]["dspy"]["signature"]["inputs"][  # type: ignore[index]
        "rubric"
    ] = "str"
    with pytest.raises(ConfigurationError, match="canonical solver"):
        WorkspaceConfiguration.from_mapping(payload)


def test_unknown_legacy_workspace_surface_is_rejected() -> None:
    payload = workspace_payload()
    payload["targets"] = {"legacy": "not-supported"}
    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)


def test_model_deployment_must_be_stored_directly() -> None:
    payload = workspace_payload()
    model = payload["models"]["solver"]  # type: ignore[index]
    del model["deployment"]  # type: ignore[index]
    model["name_env"] = "LEGACY_MODEL_SETTING"  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)


def test_schema_v7_requires_case_authoring_instructions() -> None:
    payload = workspace_payload()
    del payload["evaluation"]["case_authoring"]["instructions"]  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="case_authoring.instructions"):
        WorkspaceConfiguration.from_mapping(payload)


def test_case_authoring_accepts_immutable_reference_sources() -> None:
    payload = workspace_payload()
    payload["evaluation"]["case_authoring"]["sources"] = [  # type: ignore[index]
        {
            "id": "project-example",
            "role": "project",
            "repository": {
                "url": "https://github.com/example/project-example",
                "ref": "v2.0.0",
            },
            "include": ["README.md", "docs/**", "src/**"],
        }
    ]

    workspace = WorkspaceConfiguration.from_mapping(payload)

    assert len(workspace.evaluation.case_authoring.sources) == 1
    source = workspace.evaluation.case_authoring.sources[0]
    assert source.id == "project-example"
    assert source.role == "project"
    assert source.repository.ref == "v2.0.0"
    assert source.include == ("README.md", "docs/**", "src/**")


def test_case_authoring_reference_sources_reject_duplicates_and_unsafe_globs() -> None:
    source = {
        "id": "project-example",
        "role": "project",
        "repository": {
            "url": "https://github.com/example/project-example",
            "ref": "v2.0.0",
        },
        "include": ["docs/**"],
    }
    duplicated = workspace_payload()
    duplicated["evaluation"]["case_authoring"]["sources"] = [source, source]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="unique source ids"):
        WorkspaceConfiguration.from_mapping(duplicated)

    unsafe = workspace_payload()
    unsafe_source = dict(source)
    unsafe_source["include"] = ["../secrets/**"]
    unsafe["evaluation"]["case_authoring"]["sources"] = [unsafe_source]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="safe repository-relative glob"):
        WorkspaceConfiguration.from_mapping(unsafe)

    reserved = workspace_payload()
    reserved_source = dict(source)
    reserved_source["id"] = "target"
    reserved["evaluation"]["case_authoring"]["sources"] = [reserved_source]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="reserves the source id 'target'"):
        WorkspaceConfiguration.from_mapping(reserved)


def test_reference_source_context_is_filtered_qualified_and_content_addressed(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    external = tmp_path / "external"
    payload = workspace_payload()
    payload["workspace"]["data_root"] = external.as_posix()  # type: ignore[index]
    payload["evaluation"]["case_authoring"]["sources"] = [  # type: ignore[index]
        {
            "id": "project-example",
            "role": "project",
            "repository": {
                "url": "https://github.com/example/project-example",
                "ref": "v2.0.0",
            },
            "include": ["docs/**"],
        }
    ]
    manifest = workspace_root / "workspace.yaml"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(manifest)

    primary_checkout = create_checkout(tmp_path / "primary")
    primary, primary_directory = create_snapshot(
        repository,
        primary_checkout,
        external,
    )
    configuration, target = repository.authoring_source_specifications()[0]
    reference_checkout = tmp_path / "reference-checkout"
    (reference_checkout / "docs").mkdir(parents=True)
    (reference_checkout / "docs" / "architecture.md").write_text(
        "# Architecture\n\nUse a service boundary.\n",
        encoding="utf-8",
    )
    (reference_checkout / "private.txt").write_text("not selected\n", encoding="utf-8")
    resolved = ResolvedSource(
        target.source.repository_url,
        target.source.repository_url + ".git",
        target.source.ref,
        "b" * 40,
    )
    store = ExternalSnapshotStore(external, workspace_root=repository.root)
    reference = SnapshotBuilder(None, store).create_from_checkout(  # type: ignore[arg-type]
        target,
        resolved,
        reference_checkout,
    )
    context = load_snapshot_context(
        primary,
        primary_directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
        authoring_snapshots=((configuration, reference, store.directory(reference)),),
    )

    qualified = "references/project-example/docs/architecture.md"
    assert qualified in context.source_paths
    assert "references/project-example/private.txt" not in context.source_paths
    assert context.snapshot_hash != primary.content_hash
    assert [item.id for item in context.sources] == ["target", "project-example"]
    supporting = context.supporting_source("alpha", "service architecture")
    assert "project-example: role=project" in supporting
    assert f"# Source: {qualified}" in supporting

    revised = load_snapshot_context(
        primary,
        primary_directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
        authoring_snapshots=(
            (
                replace(configuration, include=("docs/**", "private.txt")),
                reference,
                store.directory(reference),
            ),
        ),
    )
    assert revised.snapshot_hash != context.snapshot_hash


def test_case_authoring_instructions_must_be_a_nonempty_regular_file(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    repository.case_authoring_path.unlink()

    with pytest.raises(ResolutionError, match="do not exist or are unsafe"):
        repository.case_authoring_instructions()

    (repository.root / "case-authoring.md").write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must not be empty"):
        repository.case_authoring_instructions()

    (repository.root / "case-authoring.md").write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="must be UTF-8"):
        repository.case_authoring_instructions()


def test_case_authoring_instructions_reject_symlinks(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    policy = repository.case_authoring_path
    policy.unlink()
    external = tmp_path / "external-policy.md"
    external.write_text("Do not trust this indirection.\n", encoding="utf-8")
    policy.symlink_to(external)

    with pytest.raises(ResolutionError, match="do not exist or are unsafe"):
        repository.case_authoring_instructions()


def test_case_authoring_content_changes_policy_identity(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    original = sha256_file(repository.case_authoring_path)

    repository.case_authoring_path.write_text(
        repository.case_authoring_instructions() + "\nRequire adversarial edge coverage.\n",
        encoding="utf-8",
    )
    revised = sha256_file(repository.case_authoring_path)

    assert revised != original


def test_case_authoring_policy_identity_preserves_line_ending_bytes(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    repository.case_authoring_path.write_bytes(b"Policy line one.\r\nPolicy line two.\r\n")
    windows_hash = sha256_file(repository.case_authoring_path)
    repository.case_authoring_path.write_bytes(b"Policy line one.\nPolicy line two.\n")
    unix_hash = sha256_file(repository.case_authoring_path)

    assert windows_hash != unix_hash


def test_policy_change_rejects_cases_authored_under_previous_policy(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    snapshot, snapshot_directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    environment = {"TEST_API_KEY": "test-secret"}
    seed_cases_and_judge_validation(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=snapshot.content_hash,
    )
    compiled = compile_workspace(
        repository,
        snapshot=snapshot,
        snapshot_directory=snapshot_directory,
    )

    repository.case_authoring_path.write_text(
        repository.case_authoring_instructions() + "\nUse a revised policy.\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError, match="active instructions"):
        compiled.experiment_lock("baseline", environment=environment)


def test_default_data_root_is_external_and_workspace_specific(tmp_path: Path) -> None:
    manifest = tmp_path / "workspace.yaml"
    manifest.write_text(yaml.safe_dump(workspace_payload(), sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(manifest)
    assert repository.data_root == Path.home() / "ms_agent_eval" / "synthetic-evaluation"


def test_explicit_data_root_inside_enclosing_git_repository_is_rejected(
    tmp_path: Path,
) -> None:
    git_root = tmp_path / "repository"
    (git_root / ".git").mkdir(parents=True)
    workspace_root = git_root / "experiments" / "example"
    workspace_root.mkdir(parents=True)
    payload = workspace_payload()
    payload["workspace"]["data_root"] = (git_root / "generated").as_posix()  # type: ignore[index]
    manifest = workspace_root / "workspace.yaml"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(manifest)

    with pytest.raises(ConfigurationError, match="outside the Git repository"):
        _ = repository.data_root


def test_bootstrap_inspection_reports_readiness_without_cases_or_judge_validation(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    snapshot, snapshot_directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    (repository.cases_root / "splits.yaml").write_text(
        "schema_version: 3\ngroups: {}\n", encoding="utf-8"
    )
    (repository.judge_validation_root / "manifest.yaml").write_text(
        "schema_version: 3\nfixtures: []\n", encoding="utf-8"
    )

    result = inspect_workspace(
        repository,
        environment={},
        snapshot=snapshot,
        snapshot_directory=snapshot_directory,
    )

    assert result["status"] == "incomplete"
    assert result["ready_for_scored_run"] is False
    assert result["executors"]["remote-models"]["total_concurrency"] == 1  # type: ignore[index]
    assert str(result["executors"]["remote-models"]["pool_hash"]).startswith(  # type: ignore[index]
        "sha256:"
    )
    assert result["cases"]["count"] == 0  # type: ignore[index]
    assert result["judge"]["validation"]["status"] == "incomplete"  # type: ignore[index]
    assert result["experiments"]["baseline"]["lock_hash"] is None  # type: ignore[index]


def test_snapshot_cache_is_keyed_by_canonical_url_and_resolved_tag_commit(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external"
    payload = workspace_payload()
    payload["workspace"]["data_root"] = external.as_posix()  # type: ignore[index]
    manifest = root / "workspace.yaml"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(manifest)
    checkout = create_checkout(tmp_path)
    source = repository.target_specification().source
    resolved = [
        ResolvedSource(
            source.repository_url,
            source.repository_url + ".git",
            source.ref,
            "a" * 40,
        )
    ]
    store = ExternalSnapshotStore(external, workspace_root=repository.root)
    first = SnapshotBuilder(None, store).create_from_checkout(  # type: ignore[arg-type]
        repository.target_specification(), resolved[0], checkout
    )

    class Provider:
        def resolve(self, configured):  # type: ignore[no-untyped-def]
            assert configured == source
            return resolved[0]

        def materialize(self, configured, destination):  # type: ignore[no-untyped-def]
            assert configured == resolved[0]
            shutil.copytree(checkout, destination)

    monkeypatch.setattr(planning, "GitHubSourceProvider", Provider)
    cached, _ = acquire_snapshot(repository)
    assert cached == first

    resolved[0] = ResolvedSource(
        source.repository_url,
        source.repository_url + ".git",
        source.ref,
        "b" * 40,
    )
    moved, _ = acquire_snapshot(repository)
    assert moved.resolved_commit == "b" * 40
    assert moved.content_hash != first.content_hash
