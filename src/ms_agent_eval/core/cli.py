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

    init = commands.add_parser("init", help="Create a schema-v7 staged DSPy workspace.")
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
    init.add_argument("--author-model", default="gpt-5.6-sol")

    codex = commands.add_parser(
        "codex", help="Manage subscription-backed Codex harness authentication."
    )
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    codex_login = codex_commands.add_parser(
        "login", help="Sign in with a ChatGPT/Codex subscription using device auth."
    )
    codex_login.add_argument("--auth-root", type=Path)
    codex_status = codex_commands.add_parser(
        "status", help="Check the dedicated subscription authentication store."
    )
    codex_status.add_argument("--auth-root", type=Path)
    codex_preflight = codex_commands.add_parser(
        "preflight", help="Verify subscription auth, pinned image, CLI, Python, and network."
    )
    codex_preflight.add_argument("--executor", required=True)
    codex_preflight.add_argument("--auth-root", type=Path)
    _workspace_argument(codex_preflight)

    validate = commands.add_parser("validate", help="Validate the complete workspace contract.")
    _workspace_argument(validate)

    inspect = commands.add_parser("inspect", help="Resolve and display every generated identity.")
    _workspace_argument(inspect)

    run = commands.add_parser("run", help="Start or resume a staged evaluation.")
    run.add_argument("target", help="Experiment id, or the literal 'resume'.")
    run.add_argument("run_id", nargs="?", help="Run id when target is 'resume'.")
    run.add_argument(
        "--through",
        choices=(
            "author",
            "calibrate_judge",
            "solve",
            "judge",
            "optimize",
            "final_evaluate",
            "aggregate",
        ),
    )
    _workspace_argument(run)

    stage = commands.add_parser("stage", help="Operate portable stage packets.")
    stage_commands = stage.add_subparsers(dest="stage_command", required=True)
    stage_list = stage_commands.add_parser("list", help="List packet states for a run.")
    stage_list.add_argument("--run", required=True)
    _workspace_argument(stage_list)
    prepare = stage_commands.add_parser("prepare", help="Prepare a stage independently.")
    prepare.add_argument(
        "stage_kind",
        choices=(
            "author",
            "calibrate_judge",
            "solve",
            "judge",
            "optimize",
            "final_evaluate",
            "aggregate",
        ),
    )
    prepare.add_argument("--run", required=True)
    prepare.add_argument("--campaign")
    _workspace_argument(prepare)
    claim = stage_commands.add_parser("claim", help="Claim a prepared packet lease.")
    claim.add_argument("packet_id")
    claim.add_argument("--executor", required=True)
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=float, default=600)
    _workspace_argument(claim)
    execute = stage_commands.add_parser("execute", help="Execute a packet automatically.")
    execute.add_argument("packet_id")
    execute.add_argument("--executor")
    _workspace_argument(execute)
    submit = stage_commands.add_parser("submit", help="Submit and validate harness output.")
    submit.add_argument("packet_id")
    submit.add_argument("--result", type=Path, required=True)
    submit.add_argument("--executor", required=True)
    submit.add_argument("--worker", required=True)
    submit.add_argument("--lease", required=True)
    _workspace_argument(submit)
    retry = stage_commands.add_parser("retry", help="Prepare a new immutable attempt.")
    retry.add_argument("packet_id")
    _workspace_argument(retry)
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
        author_model=arguments.author_model,
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
    if arguments.command == "codex":
        from .codex_auth import CodexSubscriptionAuthStore

        auth = CodexSubscriptionAuthStore(arguments.auth_root)
        if arguments.codex_command == "login":
            identity = auth.login()
        elif arguments.codex_command == "status":
            identity = auth.identity()
        else:
            from ms_agent_eval.adapters.codex_cli import SubprocessDockerRunner

            repository = WorkspaceRepository.from_file(arguments.workspace)
            try:
                executor = repository.workspace.executors[arguments.executor]
            except KeyError as error:
                raise ConfigurationError(
                    f"unknown Codex executor: {arguments.executor}"
                ) from error
            if executor.adapter != "codex_cli" or executor.codex_cli is None:
                raise ConfigurationError(
                    f"executor {arguments.executor!r} is not a codex_cli executor"
                )
            identity = auth.identity()
            codex = executor.codex_cli
            preflight = SubprocessDockerRunner().preflight(
                image=codex.image,
                network=codex.control_plane.docker_network,
                timeout_seconds=60,
                maximum_output_bytes=codex.resources.maximum_output_bytes,
            )
            configured = (executor.version or "").removeprefix("codex-cli ").strip()
            observed = preflight.codex_version.removeprefix("codex-cli ").strip()
            if configured != observed:
                raise ConfigurationError(
                    "Codex CLI version does not match the executor configuration"
                )
            return {
                "status": "ready",
                "executor": executor.id,
                "authentication": identity.authentication,
                "image": codex.image,
                "image_id": preflight.image_id,
                "network_id": preflight.network_id,
                "network_policy_id": codex.control_plane.policy_id,
                "codex_version": preflight.codex_version,
                "python_version": preflight.python_version,
                "container_runtime": "docker",
                "container_runtime_version": preflight.container_runtime_version,
            }
        return {
            "status": "authenticated",
            "authentication": identity.authentication,
            "codex_version": identity.codex_version,
            "auth_root": auth.root.as_posix(),
        }
    repository = WorkspaceRepository.from_file(arguments.workspace)
    from .planning import compile_workspace, inspect_workspace

    environment = repository.environment()
    if arguments.command == "validate":
        return inspect_workspace(repository, environment=environment)
    if arguments.command == "inspect":
        return inspect_workspace(repository, environment=environment)
    from .coordinator import StageCoordinator
    from .stages import StageKind

    compiled = compile_workspace(repository)
    coordinator = StageCoordinator(compiled)
    if arguments.command == "run":
        if arguments.target == "resume":
            if not arguments.run_id:
                raise ConfigurationError("run resume requires a run id")
            return json_value(
                coordinator.resume(
                    arguments.run_id,
                    environment=environment,
                    through=arguments.through,
                )
            )
        if arguments.run_id is not None:
            raise ConfigurationError("a run id is only valid with 'run resume'")
        return json_value(
            coordinator.start(
                arguments.target,
                environment=environment,
                through=arguments.through,
            )
        )
    if arguments.stage_command == "list":
        return {
            "run_id": arguments.run,
            "packets": json_value(coordinator.ledger.list(arguments.run)),
        }
    if arguments.stage_command == "prepare":
        return {
            "run_id": arguments.run,
            "packets": json_value(
                coordinator.prepare_stage(
                    arguments.run,
                    StageKind(arguments.stage_kind),
                    campaign=arguments.campaign,
                )
            ),
        }
    if arguments.stage_command == "claim":
        return json_value(
            coordinator.ledger.claim(
                arguments.packet_id,
                executor_id=arguments.executor,
                worker_id=arguments.worker,
                lease_seconds=arguments.lease_seconds,
            )
        )
    if arguments.stage_command == "execute":
        result = coordinator.execute_packet(
            arguments.packet_id,
            environment=environment,
            executor_id=arguments.executor,
        )
        if result is None:
            from .stage_executors import render_harness_prompt

            packet = coordinator.ledger.load_packet(arguments.packet_id)
            return {
                "status": "awaiting_harness",
                "packet_id": arguments.packet_id,
                "prompt": render_harness_prompt(packet, coordinator.ledger),
            }
        return json_value(result)
    if arguments.stage_command == "submit":
        return json_value(
            coordinator.submit_file(
                arguments.packet_id,
                arguments.result,
                executor_id=arguments.executor,
                worker_id=arguments.worker,
                lease_id=arguments.lease,
            )
        )
    if arguments.stage_command == "retry":
        return json_value(coordinator.ledger.retry(arguments.packet_id))
    raise ConfigurationError("unknown stage command")


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
