"""Current workspace configuration and readiness tests."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.authoring import load_snapshot_context
from ms_agent_eval.core.errors import ConfigurationError, IntegrityError, ResolutionError
from ms_agent_eval.core.hashing import sha256_file
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
    seed_cases_and_calibration,
    workspace_payload,
)


def _compiled_workspace_with_cases(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    lock, directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    seed_cases_and_calibration(
        repository,
        authoring_instructions_hash=sha256_file(repository.case_authoring_path),
        source_snapshot_hash=lock.content_hash,
    )
    return compile_workspace(
        repository,
        snapshot=lock,
        snapshot_directory=directory,
    )


def test_schema_v9_rejects_case_authoring_model_configuration() -> None:
    payload = workspace_payload()
    payload["evaluation"]["case_authoring"]["model"] = {"name": "builder"}  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="unknown fields"):
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
    payload["models"]["judge"]["name"] = "solver"  # type: ignore[index]
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
    del missing["models"]["solver"]["deployment_revision"]  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="deployment_revision"):
        WorkspaceConfiguration.from_mapping(missing)

    changed = workspace_payload()
    changed["models"]["solver"]["deployment_revision"] = "solver-test-v2"  # type: ignore[index]
    original_hash = resolve_role_models(
        WorkspaceConfiguration.from_mapping(workspace_payload()), "baseline"
    )["solver"].content_hash
    changed_hash = resolve_role_models(
        WorkspaceConfiguration.from_mapping(changed), "baseline"
    )["solver"].content_hash
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
    payload["experiments"]["optimize"]["optimizer"]["parameters"]["seed"] = 99
    workspace_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    repository = WorkspaceRepository.from_file(workspace_file)
    changed = compile_workspace(
        repository,
        snapshot=compiled.snapshot,
        snapshot_directory=compiled.snapshot_directory,
    ).experiment_lock("baseline")

    assert changed.workspace_hash != original.workspace_hash
    assert changed.content_hash == original.content_hash


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
    executor["api_key_env"] = "OPENAI_API_KEY"  # type: ignore[index]
    workspace = WorkspaceConfiguration.from_mapping(payload)
    resolved = resolve_role_models(workspace, "baseline", {})["solver"]

    with pytest.raises(ResolutionError, match="solver API credential is required"):
        resolved.api_key({})
    assert resolved.api_key({"OPENAI_API_KEY": "secret"}) == "secret"


def test_calibration_must_use_the_exact_judge_executor_and_model() -> None:
    payload = workspace_payload()
    payload["stages"]["calibrate_judge"]["model"] = "solver"  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="exact executor and model"):
        WorkspaceConfiguration.from_mapping(payload)


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


def test_model_name_and_base_url_must_be_stored_directly() -> None:
    payload = workspace_payload()
    model = payload["models"]["solver"]  # type: ignore[index]
    del model["name"]  # type: ignore[index]
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
    seed_cases_and_calibration(
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


def test_bootstrap_inspection_reports_readiness_without_cases_or_calibration(
    tmp_path: Path,
) -> None:
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

    result = inspect_workspace(
        repository,
        environment={},
        snapshot=snapshot,
        snapshot_directory=snapshot_directory,
    )

    assert result["status"] == "incomplete"
    assert result["ready_for_scored_run"] is False
    assert result["cases"]["count"] == 0  # type: ignore[index]
    assert result["judge"]["calibration"]["status"] == "incomplete"  # type: ignore[index]
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
