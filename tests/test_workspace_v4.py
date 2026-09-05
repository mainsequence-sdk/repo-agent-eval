from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.errors import ConfigurationError, IntegrityError, ResolutionError
from ms_agent_eval.core.workspace import (
    WorkspaceConfiguration,
    WorkspaceRepository,
    resolve_role_models,
)
from ms_agent_eval.programs.dspy.engine import create_case_builder_program, program_hash
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


def test_schema_v4_rejects_environment_indirected_model_configuration() -> None:
    payload = workspace_payload()
    model = payload["evaluation"]["case_builder"]["model"]  # type: ignore[index]
    model["name_env"] = "BUILDER_MODEL"  # type: ignore[index]
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


def test_configured_llm_roles_must_be_distinct() -> None:
    workspace = WorkspaceConfiguration.from_mapping(workspace_payload())
    models = resolve_role_models(
        workspace,
        "baseline",
        {"UNRELATED_PROCESS_SETTING": "cannot-override-workspace-models"},
    )
    assert set(models) == {"case_builder", "solver", "judge"}
    assert {role: model.model for role, model in models.items()} == {
        "case_builder": "builder",
        "solver": "solver",
        "judge": "judge",
    }
    payload = workspace_payload()
    payload["evaluation"]["judge"]["model"]["name"] = "solver"  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="three distinct"):
        WorkspaceConfiguration.from_mapping(payload)


def test_configured_api_key_reference_is_required_only_at_request_time() -> None:
    payload = workspace_payload()
    model = payload["experiments"]["baseline"]["solver"]["model"]  # type: ignore[index]
    model["api_key_env"] = "OPENAI_API_KEY"  # type: ignore[index]
    workspace = WorkspaceConfiguration.from_mapping(payload)
    resolved = resolve_role_models(workspace, "baseline", {})["solver"]

    with pytest.raises(ResolutionError, match="solver API credential is required"):
        resolved.api_key({})
    assert resolved.api_key({"OPENAI_API_KEY": "secret"}) == "secret"


def test_role_reuse_is_rejected_even_when_endpoints_differ() -> None:
    payload = workspace_payload()
    payload["experiments"]["baseline"]["solver"]["model"]["name"] = "shared"  # type: ignore[index]
    payload["evaluation"]["judge"]["model"]["name"] = "shared"  # type: ignore[index]
    payload["evaluation"]["judge"]["model"]["base_url"] = "http://remote.example/v1"  # type: ignore[index]
    with pytest.raises(ConfigurationError, match="three distinct"):
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
    model = payload["evaluation"]["case_builder"]["model"]  # type: ignore[index]
    del model["name"]  # type: ignore[index]
    model["name_env"] = "LEGACY_MODEL_SETTING"  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="unknown fields"):
        WorkspaceConfiguration.from_mapping(payload)


def test_schema_v4_requires_case_authoring_instructions() -> None:
    payload = workspace_payload()
    del payload["evaluation"]["case_builder"]["instructions"]  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="case_builder.instructions"):
        WorkspaceConfiguration.from_mapping(payload)


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


def test_case_authoring_content_changes_builder_program_identity(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    original = program_hash(
        create_case_builder_program(repository.case_authoring_instructions())
    )

    repository.case_authoring_path.write_text(
        repository.case_authoring_instructions() + "\nRequire adversarial edge coverage.\n",
        encoding="utf-8",
    )
    revised = program_hash(
        create_case_builder_program(repository.case_authoring_instructions())
    )

    assert revised != original


def test_case_authoring_program_identity_preserves_line_ending_bytes(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    repository.case_authoring_path.write_bytes(b"Policy line one.\r\nPolicy line two.\r\n")
    windows_hash = program_hash(
        create_case_builder_program(repository.case_authoring_instructions())
    )
    repository.case_authoring_path.write_bytes(b"Policy line one.\nPolicy line two.\n")
    unix_hash = program_hash(
        create_case_builder_program(repository.case_authoring_instructions())
    )

    assert windows_hash != unix_hash


def test_policy_change_rejects_cases_built_by_previous_builder_program(
    tmp_path: Path,
) -> None:
    repository = create_workspace(tmp_path / "workspace")
    snapshot, snapshot_directory = create_snapshot(
        repository,
        create_checkout(tmp_path),
        tmp_path / "external",
    )
    environment = {"TEST_API_KEY": "test-secret"}
    builder_model = resolve_role_models(repository.workspace, "baseline", environment)[
        "case_builder"
    ]
    seed_cases_and_calibration(
        repository,
        builder_model_hash=builder_model.content_hash,
        builder_program_hash=program_hash(
            create_case_builder_program(repository.case_authoring_instructions())
        ),
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

    with pytest.raises(IntegrityError, match="active DSPy builder program"):
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
