from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ms_agent_eval.ui import QuestionaryPromptBackend

from .errors import AgentEvalError, ConfigurationError, IntegrityError
from .hashing import json_value
from .setup import run_guided_setup
from .workspace import (
    ExperimentMode,
    WorkspaceConfiguration,
    WorkspaceRepository,
    resolve_role_models,
)
from .workspace_templates import build_workspace_template, write_workspace_template

if TYPE_CHECKING:
    from .case_builder import CaseDraftStore


def _workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path("workspace.yaml"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ms-agent-eval",
        description="Build, optimize, and evaluate DSPy repository agents with three LLM roles.",
    )
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser("setup", help="Interactively create or inspect a workspace.")
    setup.add_argument("--workspace", type=Path)

    init = commands.add_parser("init", help="Create a schema-v4 DSPy workspace.")
    init.add_argument("--id", required=True)
    init.add_argument("--repo", required=True)
    init.add_argument("--ref", required=True)
    init.add_argument("--global-instructions", action="append", required=True)
    skills = init.add_mutually_exclusive_group(required=True)
    skills.add_argument("--skills-directory")
    skills.add_argument("--skill-file", action="append")
    init.add_argument("--cases", type=Path, default=Path("cases"))
    init.add_argument("--workspace", type=Path, default=Path("workspace.yaml"))
    init.add_argument("--api-base-url", required=True)
    init.add_argument("--api-key-env")
    init.add_argument("--case-builder-model", required=True)
    init.add_argument("--solver-model", required=True)
    init.add_argument("--judge-model", required=True)

    validate = commands.add_parser("validate", help="Validate the complete workspace contract.")
    _workspace_argument(validate)

    inspect = commands.add_parser("inspect", help="Resolve and display every generated identity.")
    _workspace_argument(inspect)

    cases = commands.add_parser("cases", help="Build and explicitly promote LLM-authored cases.")
    case_commands = cases.add_subparsers(dest="case_command", required=True)
    build = case_commands.add_parser("build", help="Build external case drafts with DSPy.")
    _workspace_argument(build)
    build.add_argument("--coverage", required=True)
    build.add_argument("--skill", action="append")
    drafts = case_commands.add_parser(
        "inspect-drafts", help="List external drafts without modifying the case bank."
    )
    _workspace_argument(drafts)
    promote = case_commands.add_parser("promote", help="Promote validated drafts explicitly.")
    _workspace_argument(promote)
    promote.add_argument("--draft", action="append")

    run = commands.add_parser("run", help="Run a locked DSPy evaluation or optimization.")
    run.add_argument("experiment_id")
    _workspace_argument(run)
    return parser


def _init(arguments: argparse.Namespace) -> Mapping[str, object]:
    workspace_file = arguments.workspace.resolve()
    root = workspace_file.parent
    cases = arguments.cases
    if cases.is_absolute():
        try:
            cases = cases.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise ConfigurationError("--cases must be inside the workspace") from error
    skills_block: dict[str, object]
    if arguments.skills_directory:
        skills_block = {"directory": arguments.skills_directory}
    else:
        skills_block = {"files": arguments.skill_file}
    template = build_workspace_template(
        workspace_id=arguments.id,
        repository_url=arguments.repo,
        repository_ref=arguments.ref,
        global_instructions=arguments.global_instructions,
        skills=skills_block,
        cases_directory=cases.as_posix(),
        api_base_url=arguments.api_base_url,
        api_key_env=arguments.api_key_env,
        case_builder_model=arguments.case_builder_model,
        solver_model=arguments.solver_model,
        judge_model=arguments.judge_model,
    )
    WorkspaceConfiguration.from_mapping(template.payload)
    paths = write_workspace_template(workspace_file, template)
    repository = WorkspaceRepository.from_file(workspace_file)
    return {
        "status": "initialized",
        **paths,
        "data_root": repository.data_root.as_posix(),
    }


def _draft_store(repository: WorkspaceRepository) -> CaseDraftStore:
    from .case_builder import CaseDraftStore
    from .storage import FilesystemArtifactStore

    artifacts = FilesystemArtifactStore(repository.data_root, workspace_root=repository.root)
    return CaseDraftStore(
        repository.data_root,
        workspace_root=repository.root,
        artifacts=artifacts,
    )


def _cases(arguments: argparse.Namespace, repository: WorkspaceRepository) -> object:
    from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits
    from ms_agent_eval.programs.dspy.engine import (
        create_case_builder_program,
        program_hash,
    )
    from ms_agent_eval.providers.openai_compatible import create_observed_lm

    from .case_builder import CaseBuilderService, load_snapshot_context
    from .planning import acquire_snapshot

    drafts = _draft_store(repository)
    if arguments.case_command == "inspect-drafts":
        return {
            "drafts": [
                {
                    "id": item.id,
                    "case_id": item.case_id,
                    "skill": item.skill,
                    "status": item.status,
                    "package_hash": item.package_hash,
                    "source_snapshot_hash": item.source_snapshot_hash,
                }
                for item in drafts.list()
            ]
        }
    if arguments.case_command == "promote":
        selected = set(arguments.draft or ())
        candidates = [
            item
            for item in drafts.list()
            if item.status == "validated" and (not selected or item.id in selected)
        ]
        if selected - {item.id for item in candidates}:
            raise IntegrityError(
                f"unknown or non-validated drafts: {sorted(selected - {item.id for item in candidates})}"
            )
        if not candidates:
            raise IntegrityError("there are no validated case drafts to promote")
        environment = repository.environment()
        models = resolve_role_models(repository.workspace, _baseline_id(repository), environment)
        snapshot, _ = acquire_snapshot(repository)
        active_program_hash = program_hash(
            create_case_builder_program(repository.case_authoring_instructions())
        )
        for item in candidates:
            if item.source_snapshot_hash != snapshot.content_hash:
                raise IntegrityError(f"draft {item.id!r} was built from a stale source snapshot")
            if item.builder_model_hash != models["case_builder"].content_hash:
                raise IntegrityError(
                    f"draft {item.id!r} was built by a different case-builder model"
                )
            if item.builder_program_hash != active_program_hash:
                raise IntegrityError(
                    f"draft {item.id!r} was built by a different DSPy builder program"
                )
        repository.cases_root.mkdir(parents=True, exist_ok=True)
        promoted = [drafts.promote(item.id, repository.cases_root) for item in candidates]
        _update_splits(repository, promoted)
        return {
            "status": "promoted",
            "cases": [
                {"id": item.id, "skill": item.skill, "group": item.group} for item in promoted
            ],
        }
    snapshot, directory = acquire_snapshot(repository)
    context = load_snapshot_context(
        snapshot,
        directory,
        global_paths=repository.workspace.evaluation.instructions.global_paths,
    )
    environment = repository.environment()
    model = resolve_role_models(repository.workspace, _baseline_id(repository), environment)[
        "case_builder"
    ]
    artifacts = drafts.artifacts
    service = CaseBuilderService(repository, context, artifacts, drafts)
    budget = repository.workspace.evaluation.case_builder.budget
    ledger = BudgetLedger(
        BudgetLimits(
            model_calls=budget.model_calls,
            configured_cost=float("inf"),
            tokens=budget.tokens,
            wall_seconds=budget.wall_seconds,
            concurrency=1,
        )
    )
    selected_skills = tuple(arguments.skill or context.skill_contexts)
    unknown = sorted(set(selected_skills) - set(context.skill_contexts))
    if unknown:
        raise ConfigurationError(f"unknown skills requested for case building: {unknown}")
    built = [
        service.build(
            skill=skill,
            coverage_request=arguments.coverage,
            model=model,
            lm_factory=lambda observer: create_observed_lm(
                model,
                observer,
                budget=ledger,
                api_key=model.api_key(environment),
            ),
        )
        for skill in selected_skills
    ]
    return {
        "status": "drafted",
        "source_snapshot_hash": snapshot.content_hash,
        "drafts": [{"id": item.id, "case_id": item.case_id, "skill": item.skill} for item in built],
        "budget": json_value(ledger.snapshot()),
    }


def _baseline_id(repository: WorkspaceRepository) -> str:
    for experiment_id, experiment in repository.workspace.experiments.items():
        if experiment.mode is ExperimentMode.EVALUATE:
            return experiment_id
    raise ConfigurationError("case workflow requires an evaluate experiment")


def _update_splits(repository: WorkspaceRepository, promoted: Sequence[object]) -> None:
    split_file = repository.split_file
    if split_file.exists():
        payload = yaml.safe_load(split_file.read_text(encoding="utf-8"))
    else:
        payload = {"schema_version": 2, "groups": {}}
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ConfigurationError("split file must use schema_version 2", path=split_file)
    groups = payload.get("groups")
    if not isinstance(groups, dict):
        raise ConfigurationError("split groups must be a mapping", path=split_file)
    policy = repository.workspace.evaluation.splits.policy
    thresholds = (
        (policy.train, "train"),
        (policy.train + policy.development, "development"),
        (policy.train + policy.development + policy.test, "test"),
        (1.0, "challenge"),
    )
    for case in promoted:
        group = getattr(case, "group")
        if group in groups:
            continue
        digest = hashlib.sha256(f"{policy.seed}:{group}".encode()).digest()
        point = int.from_bytes(digest[:8], "big") / 2**64
        groups[group] = next(name for threshold, name in thresholds if point < threshold)
    _atomic_text(split_file, yaml.safe_dump(payload, sort_keys=False))


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _run(arguments: argparse.Namespace) -> object:
    if arguments.command == "setup":
        return run_guided_setup(
            QuestionaryPromptBackend(), workspace_file=arguments.workspace
        )
    if arguments.command == "init":
        return _init(arguments)
    repository = WorkspaceRepository.from_file(arguments.workspace)
    if arguments.command == "cases":
        return _cases(arguments, repository)
    from .planning import compile_workspace, inspect_workspace

    environment = repository.environment()
    if arguments.command == "validate":
        return inspect_workspace(repository, environment=environment)
    if arguments.command == "inspect":
        return inspect_workspace(repository, environment=environment)
    from .runner import ExperimentRunner

    compiled = compile_workspace(repository)
    return json_value(
        ExperimentRunner(compiled).run(arguments.experiment_id, environment=environment)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            arguments = parser.parse_args(["setup"])
        else:
            parser.print_help(sys.stderr)
            return 2
    try:
        payload = _run(arguments)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except AgentEvalError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
