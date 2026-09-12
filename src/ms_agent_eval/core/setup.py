from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import yaml

from ms_agent_eval.ui import Choice, PromptBackend

from .discovery import InspectedRepository, discover_tags, inspect_repository
from .errors import AgentEvalError, ConfigurationError, IntegrityError
from .model_catalog import ModelCatalog, list_models, validate_api_base_url
from .snapshots import (
    ExternalSnapshotStore,
    SnapshotBuilder,
    validate_instruction_selection,
)
from .sources import GitHubSourceProvider
from .workspace import (
    WorkspaceConfiguration,
    WorkspaceRepository,
    validate_external_data_root,
)
from .workspace_templates import (
    build_workspace_template,
    planned_workspace_paths,
    write_workspace_template,
)


TagLoader = Callable[[str], tuple[str, ...]]
RepositoryInspector = Callable[..., AbstractContextManager[InspectedRepository]]
ModelLoader = Callable[..., ModelCatalog]


@dataclass(frozen=True)
class SetupDependencies:
    tags: TagLoader = discover_tags
    inspect: RepositoryInspector = inspect_repository
    models: ModelLoader = list_models


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return result or "evaluation"


def _manual_paths(prompt: PromptBackend, message: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in prompt.text(message).split(",") if item.strip())
    if not values:
        raise ConfigurationError("at least one repository-relative path is required")
    return values


def _validate_instruction_paths(checkout: Path, paths: tuple[str, ...]) -> None:
    root = checkout.resolve()
    for value in paths:
        path = checkout / value
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise ConfigurationError(
                f"global instruction must be a regular repository file: {value}"
            )


def _positive_integer(value: str, field: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{field} must be a positive integer") from error
    if result < 1:
        raise ConfigurationError(f"{field} must be a positive integer")
    return result


def _models(
    prompt: PromptBackend,
    dependencies: SetupDependencies,
    *,
    base_url: str,
    api_key: str | None,
) -> tuple[str, str]:
    discovered: tuple[str, ...] = ()
    if prompt.confirm("Test the API connection and list available models?", default=True):
        try:
            discovered = dependencies.models(base_url, api_key=api_key).models
            prompt.write(f"Discovered {len(discovered)} model(s).")
        except AgentEvalError as error:
            prompt.write(f"Model discovery unavailable: {error}. Enter model ids manually.")
    roles = ("solver", "judge")
    selected: list[str] = []
    for role in roles:
        available = discovered
        if available:
            value = prompt.select(
                f"Select the {role} model",
                tuple(Choice(item, item) for item in available),
            )
        else:
            value = prompt.text(f"Enter the {role} model id")
        while not value:
            prompt.write(f"The {role} model must be non-empty.")
            value = prompt.text(f"Enter the {role} model id")
        selected.append(value)
    return selected[0], selected[1]


def _checklist(repository: WorkspaceRepository) -> Mapping[str, object]:
    cases = len(repository.case_paths()) if repository.cases_root.exists() else 0
    calibration_manifest = repository.calibration_root / "manifest.yaml"
    calibration = False
    if calibration_manifest.is_file():
        payload = yaml.safe_load(calibration_manifest.read_text(encoding="utf-8"))
        fixtures = payload.get("fixtures", []) if isinstance(payload, dict) else []
        if not isinstance(fixtures, list):
            fixtures = []
        labels = {
            fixture.get("label")
            for fixture in fixtures
            if isinstance(fixture, dict) and isinstance(fixture.get("label"), str)
        }
        calibration = {
            "strong",
            "partial",
            "incorrect",
            "contradictory",
            "adversarial",
        } <= labels
    if cases == 0:
        next_command = (
            "ask Codex to use the case-authoring skill with "
            f"{repository.workspace_file} and create the required cases, splits, "
            "and judge calibration"
        )
    elif not calibration:
        next_command = (
            "ask Codex to continue the case-authoring campaign for "
            f"{repository.workspace_file} and create the mandatory judge calibration"
        )
    else:
        next_command = f"ms-agent-eval validate --workspace {repository.workspace_file}"
    inputs_ready = cases > 0 and calibration
    has_optimization = any(
        experiment.mode.value == "optimize"
        for experiment in repository.workspace.experiments.values()
    )
    return {
        "source": "configured",
        "instructions": "configured",
        "promoted_cases": cases,
        "judge_calibration": "configured" if calibration else "missing",
        "baseline": "validate" if inputs_ready else "blocked",
        "optimization": (
            "validate" if inputs_ready else "blocked"
        )
        if has_optimization
        else "not_configured",
        "next": next_command,
    }


def _show_checklist(prompt: PromptBackend, checklist: Mapping[str, object]) -> None:
    prompt.write("\nWorkspace readiness")
    prompt.write("[configured] immutable source")
    prompt.write("[configured] global instructions and skill selection")
    cases = int(checklist["promoted_cases"])
    prompt.write(f"[{'ready' if cases else 'missing'}] promoted cases ({cases})")
    calibration = str(checklist["judge_calibration"])
    prompt.write(f"[{'ready' if calibration == 'configured' else 'missing'}] judge calibration")
    prompt.write(f"[{checklist['baseline']}] baseline run")
    if checklist["optimization"] != "not_configured":
        prompt.write(f"[{checklist['optimization']}] optimization run")
    prompt.write(f"Next: {checklist['next']}")


def run_guided_setup(
    prompt: PromptBackend,
    *,
    workspace_file: Path | None = None,
    dependencies: SetupDependencies | None = None,
) -> Mapping[str, object]:
    dependencies = dependencies or SetupDependencies()
    if workspace_file is None:
        directory = Path(prompt.text("Workspace directory", default="evaluation")).expanduser()
        workspace_file = directory / "workspace.yaml"
    workspace_file = workspace_file.expanduser().resolve()
    if workspace_file.exists():
        repository = WorkspaceRepository.from_file(workspace_file)
        prompt.write(f"Opened existing workspace: {workspace_file}")
        checklist = _checklist(repository)
        _show_checklist(prompt, checklist)
        return {"status": "existing", "workspace": workspace_file.as_posix(), **checklist}

    workspace_id = prompt.text(
        "Workspace id", default=_slug(workspace_file.parent.name)
    )
    default_data_root = Path.home() / "ms_agent_eval" / workspace_id
    data_root = validate_external_data_root(
        Path(prompt.text("External data root", default=default_data_root.as_posix())),
        workspace_file.parent,
    )
    prompt.write(f"Workspace: {workspace_file}")
    prompt.write(f"External data: {data_root}")

    repository_url = prompt.text("GitHub repository URL")
    try:
        tags = dependencies.tags(repository_url)
    except AgentEvalError as error:
        prompt.write(f"Tag discovery unavailable: {error}. Enter the revision manually.")
        tags = ()
    revision_choice = prompt.select(
        "Select an immutable revision",
        (
            Choice("Enter a full commit SHA or tag manually", "__manual__"),
            *(Choice(tag, tag) for tag in tags),
        ),
    )
    repository_ref = (
        prompt.text("Full commit SHA or tag")
        if revision_choice == "__manual__"
        else revision_choice
    )

    temporary_root = data_root / "tmp"
    with dependencies.inspect(
        repository_url,
        repository_ref,
        temporary_root=temporary_root,
    ) as inspected:
        inventory = inspected.inventory
        if inventory.global_instruction_candidates:
            global_instructions = prompt.checkbox(
                "Select global instruction files",
                tuple(Choice(item, item) for item in inventory.global_instruction_candidates),
            )
            if not global_instructions:
                global_instructions = _manual_paths(
                    prompt, "Repository-relative global instruction paths (comma-separated)"
                )
        else:
            global_instructions = _manual_paths(
                prompt, "Repository-relative global instruction paths (comma-separated)"
            )
        _validate_instruction_paths(inspected.checkout, global_instructions)

        if not inventory.skill_files:
            raise ConfigurationError("the selected repository contains no SKILL.md files")
        skill_mode = prompt.select(
            "How should skills be selected?",
            (
                Choice("Entire folder", "directory"),
                Choice("Exact list of SKILL.md files", "files"),
            ),
        )
        if skill_mode == "directory":
            directory = prompt.select(
                "Select the skill folder",
                tuple(
                    Choice(f"{item.path} ({item.skill_count} skills)", item.path)
                    for item in inventory.skill_directories
                ),
            )
            skills: Mapping[str, object] = {"directory": directory}
            selected_skills = tuple(
                item
                for item in inventory.skill_files
                if item.startswith(f"{directory.rstrip('/')}/")
            )
        else:
            selected_skills = prompt.checkbox(
                "Select the exact SKILL.md files",
                tuple(Choice(item, item) for item in inventory.skill_files),
            )
            if not selected_skills:
                raise ConfigurationError("at least one SKILL.md file must be selected")
            skills = {"files": list(selected_skills)}

        api_base_url = validate_api_base_url(
            prompt.text("OpenAI-compatible API base URL")
        )
        use_api_key = prompt.confirm("Does this API require a key?", default=False)
        api_key = prompt.secret("API key") if use_api_key else None
        api_key_env = "OPENAI_API_KEY" if use_api_key else None
        solver_model, judge_model = _models(
            prompt,
            dependencies,
            base_url=api_base_url,
            api_key=api_key,
        )
        baseline_repetitions = _positive_integer(
            prompt.text("Baseline repetitions", default="1"), "baseline repetitions"
        )
        judge_repetitions = _positive_integer(
            prompt.text("Judge repetitions", default="3"), "judge repetitions"
        )

        runtime_type = prompt.select(
            "Target execution runtime",
            (
                Choice("Response only (no container)", "response_only"),
                Choice("Docker (isolated, digest-pinned)", "docker"),
            ),
        )
        runtime_image = None
        runtime_network = "none"
        if runtime_type == "docker":
            runtime_image = prompt.text("Docker image pinned with @sha256:...")
            runtime_network = prompt.select(
                "Container network policy",
                (Choice("No network", "none"), Choice("Restricted", "restricted")),
            )
        runtime_python = prompt.text("Target Python version", default="3.12")
        cases_directory = prompt.text("Case directory", default="cases")
        coverage_request = prompt.text(
            "Initial case campaign request",
            default="Create distinct foundational application cases for every selected skill",
        )
        include_optimization = prompt.confirm(
            "Create a separate LabeledFewShot optimization experiment?", default=True
        )
        explicit_data_root = (
            None if data_root == default_data_root.resolve() else data_root.as_posix()
        )
        template = build_workspace_template(
            workspace_id=workspace_id,
            data_root=explicit_data_root,
            repository_url=repository_url,
            repository_ref=repository_ref,
            global_instructions=global_instructions,
            skills=skills,
            cases_directory=cases_directory,
            api_base_url=api_base_url,
            api_key_env=api_key_env,
            solver_model=solver_model,
            judge_model=judge_model,
            author_model="gpt-5.6-sol",
            runtime_type=runtime_type,
            runtime_python=runtime_python,
            runtime_image=runtime_image,
            runtime_network=runtime_network,
            baseline_repetitions=baseline_repetitions,
            judge_repetitions=judge_repetitions,
            include_optimization=include_optimization,
        )
        configuration = WorkspaceConfiguration.from_mapping(template.payload)
        preview_repository = WorkspaceRepository(
            workspace_file,
            workspace_file.parent,
            configuration,
        )
        validated_units = validate_instruction_selection(
            preview_repository.target_specification(), inspected.checkout
        )
        planned_paths = planned_workspace_paths(
            workspace_file, template, include_api_key=bool(api_key)
        )
        conflicts = tuple(path for path in planned_paths if path.exists())
        if conflicts:
            raise IntegrityError(
                f"setup refuses to overwrite existing file: {conflicts[0]}"
            )
        prompt.write("\nConfiguration review")
        prompt.write(f"Repository: {repository_url}")
        prompt.write(f"Requested ref: {repository_ref}")
        prompt.write(f"Resolved commit: {inspected.resolved.resolved_commit}")
        prompt.write(f"Global instructions: {', '.join(global_instructions)}")
        prompt.write(f"Skill selection mode: {skill_mode}")
        prompt.write(f"Selected skills: {len(validated_units)}")
        for unit in validated_units:
            prompt.write(f"  - {unit.unit_id}: {unit.source_path}")
        prompt.write(f"API base URL: {api_base_url}")
        prompt.write(
            "Stage models: "
            f"author=gpt-5.6-sol, solver={solver_model}, judge={judge_model}"
        )
        prompt.write("Case authoring executor: artifact-exchange harness")
        runtime_detail = runtime_type
        if runtime_image is not None:
            runtime_detail += f", image={runtime_image}, network={runtime_network}"
        prompt.write(f"Runtime: {runtime_detail}, Python {runtime_python}")
        prompt.write(
            f"Repetitions: baseline={baseline_repetitions}, judge={judge_repetitions}"
        )
        prompt.write("Configured call ceilings: optimizer solver=100, judge=300")
        prompt.write(f"Initial coverage: {coverage_request}")
        prompt.write(f"Workspace: {workspace_file}")
        prompt.write(f"External data: {data_root}")
        prompt.write(f"External snapshot destination: {data_root / 'snapshots'}")
        prompt.write("Files to create:")
        for path in planned_paths:
            prompt.write(f"  - {path}")
        prompt.write("\nworkspace.yaml\n" + template.workspace_yaml)
        if api_key:
            prompt.write("Local credential: OPENAI_API_KEY=[REDACTED]")
        if not prompt.confirm("Write this workspace?", default=False):
            return {"status": "cancelled", "workspace": workspace_file.as_posix()}

        snapshot_store = ExternalSnapshotStore(
            data_root, workspace_root=workspace_file.parent
        )
        snapshot = SnapshotBuilder(GitHubSourceProvider(), snapshot_store).create_from_checkout(
            preview_repository.target_specification(), inspected.resolved, inspected.checkout
        )
        paths = write_workspace_template(workspace_file, template, api_key=api_key)
        repository = WorkspaceRepository.from_file(workspace_file)
        checklist = _checklist(repository)
        _show_checklist(prompt, checklist)
        return {
            "status": "configured",
            **paths,
            "data_root": repository.data_root.as_posix(),
            "resolved_commit": snapshot.resolved_commit,
            "skill_count": len(snapshot.units),
            "suggested_coverage": coverage_request,
            "readiness": checklist,
        }
