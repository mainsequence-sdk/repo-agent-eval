from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ms_agent_eval.ui import QuestionaryPromptBackend

from .errors import AgentEvalError, ConfigurationError
from .hashing import json_value
from .setup import run_guided_setup
from .workspace import WorkspaceConfiguration, WorkspaceRepository
from .workspace_templates import build_workspace_template, write_workspace_template

def _workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path("workspace.yaml"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ms-agent-eval",
        description="Optimize and evaluate repository agents with DSPy solver and judge LLMs.",
    )
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser("setup", help="Interactively create or inspect a workspace.")
    setup.add_argument("--workspace", type=Path)

    init = commands.add_parser("init", help="Create a schema-v6 DSPy workspace.")
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
    init.add_argument("--solver-model", required=True)
    init.add_argument("--judge-model", required=True)

    validate = commands.add_parser("validate", help="Validate the complete workspace contract.")
    _workspace_argument(validate)

    inspect = commands.add_parser("inspect", help="Resolve and display every generated identity.")
    _workspace_argument(inspect)

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


def _run(arguments: argparse.Namespace) -> object:
    if arguments.command == "setup":
        return run_guided_setup(
            QuestionaryPromptBackend(), workspace_file=arguments.workspace
        )
    if arguments.command == "init":
        return _init(arguments)
    repository = WorkspaceRepository.from_file(arguments.workspace)
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
