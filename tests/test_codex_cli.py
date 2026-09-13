from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ms_agent_eval.adapters.codex_cli import (
    CodexCliExecutor,
    ContainerOutcome,
    ContainerPreflight,
    _codex_config,
    _usage,
)
from ms_agent_eval.core.codex_auth import (
    CodexSubscriptionAuthStore,
    SubscriptionAuthIdentity,
)
from ms_agent_eval.core.errors import ConfigurationError, IntegrityError, PreflightError
from ms_agent_eval.core.stage_views import StageViewMaterializer
from ms_agent_eval.core.stages import (
    StageBudget,
    StageExecutorIdentity,
    StageKind,
    StageLedger,
    StageModelIdentity,
    StagePacket,
)
from ms_agent_eval.core.storage import FilesystemArtifactStore
from ms_agent_eval.core.workspace import ExecutorConfiguration

from .helpers import create_case, create_workspace


def _executor() -> ExecutorConfiguration:
    return ExecutorConfiguration.from_mapping(
        "isolated-codex",
        {
            "type": "harness",
            "adapter": "codex_cli",
            "version": "0.153.1",
            "options": {
                "image": f"ghcr.io/example/codex@sha256:{'a' * 64}",
                "authentication": "chatgpt_subscription",
                "permission_profile": "evaluator",
                "instruction_policy": "controlled",
                "control_plane": {
                    "docker_network": "agent-eval-control",
                    "https_proxy": "http://codex-proxy:3128",
                    "policy_id": "openai-control-plane-v1",
                },
                "resources": {
                    "cpus": 2,
                    "memory_mb": 4096,
                    "pids": 256,
                    "maximum_output_bytes": 1048576,
                },
            },
        },
    )


def _solve_packet(ledger: StageLedger, case: dict[str, object]) -> StagePacket:
    return StagePacket.create(
        run_id="run-1",
        experiment_id="baseline",
        stage=StageKind.SOLVE,
        attempt=1,
        dependencies=(),
        inputs=(ledger.put_json("case", case),),
        instructions=ledger.put_json("instructions", {"task": "solve"}),
        output_schema="solver-response-v1",
        executor=StageExecutorIdentity(
            "isolated-codex", "codex_cli", f"sha256:{'1' * 64}"
        ),
        model=StageModelIdentity("solver", f"sha256:{'2' * 64}"),
        budget=StageBudget(1, 1000, 60),
    )


def _case_input() -> dict[str, object]:
    return {
        "case_id": "example",
        "case_hash": f"sha256:{'3' * 64}",
        "skill": "alpha",
        "split": "test",
        "repetition": 1,
        "global_context": "Global context.",
        "skill_context": "Skill context.",
        "task": "Solve this task.",
        "solver_attachments": {"fixture.txt": "public"},
        "program_hash": f"sha256:{'4' * 64}",
    }


def test_codex_configuration_requires_subscription_auth_and_digest() -> None:
    executor = _executor()
    assert executor.codex_cli is not None
    assert executor.codex_cli.authentication == "chatgpt_subscription"

    payload = {
        "type": "harness",
        "adapter": "codex_cli",
        "version": "0.153.1",
        "options": {
            **{
                "image": f"ghcr.io/example/codex@sha256:{'a' * 64}",
                "authentication": "api_key",
                "permission_profile": "evaluator",
                "instruction_policy": "controlled",
                "control_plane": {
                    "docker_network": "agent-eval-control",
                    "https_proxy": "http://codex-proxy:3128",
                    "policy_id": "openai-control-plane-v1",
                },
                "resources": {
                    "cpus": 2,
                    "memory_mb": 4096,
                    "pids": 256,
                    "maximum_output_bytes": 1048576,
                },
            }
        },
    }
    with pytest.raises(ConfigurationError, match="chatgpt_subscription"):
        ExecutorConfiguration.from_mapping("bad-auth", payload)
    payload["options"]["authentication"] = "chatgpt_subscription"
    payload["options"]["image"] = "ghcr.io/example/codex:latest"
    with pytest.raises(ConfigurationError, match="sha256 digest"):
        ExecutorConfiguration.from_mapping("mutable-image", payload)


def test_solver_stage_view_excludes_judge_material(tmp_path: Path) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = _solve_packet(ledger, _case_input())
    view = StageViewMaterializer().materialize(
        packet,
        ledger,
        tmp_path / "view",
        instruction_policy="controlled",
    )
    paths = {item.path for item in view.files}
    assert "input/task.md" in paths
    assert "input/attachments/fixture.txt" in paths
    assert not any("rubric" in path or "expected" in path for path in paths)
    assert not (view.root / "case.yaml").exists()
    assert (view.root / "AGENTS.md").stat().st_mode & 0o222 == 0


def test_solver_stage_view_rejects_hidden_fields_and_instruction_attachments(
    tmp_path: Path,
) -> None:
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    hidden = {**_case_input(), "rubric": "secret"}
    with pytest.raises(IntegrityError, match="forbidden fields"):
        StageViewMaterializer().materialize(
            _solve_packet(ledger, hidden),
            ledger,
            tmp_path / "hidden",
            instruction_policy="controlled",
        )
    instruction = {**_case_input(), "solver_attachments": {"AGENTS.md": "override"}}
    with pytest.raises(IntegrityError, match="instruction file"):
        StageViewMaterializer().materialize(
            _solve_packet(ledger, instruction),
            ledger,
            tmp_path / "instruction",
            instruction_policy="controlled",
        )


def test_case_package_rejects_undeclared_files(tmp_path: Path) -> None:
    repository = create_workspace(tmp_path / "workspace")
    case = create_case(
        repository.cases_root,
        case_id="no-hidden-files",
        skill="alpha",
        group="group-1",
        source_path="skills/alpha/SKILL.md",
    )
    (case.path / "notes.md").write_text("undeclared", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="undeclared files"):
        type(case).load(case.path)


def test_subscription_auth_store_copies_only_attempt_auth(tmp_path: Path) -> None:
    root = tmp_path / "subscription"
    root.mkdir(mode=0o700)
    (root / "auth.json").write_text('{"tokens":"secret-token"}', encoding="utf-8")
    (root / "auth.json").chmod(0o600)
    (root / "subscription.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "authentication": "chatgpt_subscription",
                "codex_version": "codex-cli 0.153.1",
            }
        ),
        encoding="utf-8",
    )
    (root / "subscription.json").chmod(0o600)
    store = CodexSubscriptionAuthStore(root)
    identity = store.install_into(tmp_path / "attempt")
    assert identity.authentication == "chatgpt_subscription"
    assert {item.name for item in (tmp_path / "attempt").iterdir()} == {"auth.json"}
    assert (tmp_path / "attempt" / "auth.json").stat().st_mode & 0o077 == 0
    assert store.redact(b"value=secret-token") == b"value=[REDACTED]"

    (root / "auth.json").chmod(0o644)
    with pytest.raises(PreflightError, match="group/world"):
        store.identity()

    (root / "auth.json").write_text(
        '{"OPENAI_API_KEY":"sk-api-billed","tokens":null}', encoding="utf-8"
    )
    (root / "auth.json").chmod(0o600)
    with pytest.raises(PreflightError, match="API-key"):
        store.identity()


def test_codex_command_has_only_stage_and_attempt_mounts(tmp_path: Path) -> None:
    configuration = _executor()
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = _solve_packet(ledger, _case_input())
    executor = CodexCliExecutor(None, None, configuration)  # type: ignore[arg-type]
    arguments = executor._docker_arguments(
        packet,
        tmp_path / "view",
        tmp_path / "codex-home",
        tmp_path / "codex-home" / "result.json",
        "agent-eval-test",
    )
    command = " ".join(arguments)
    assert "--read-only" in arguments
    assert "--cap-drop=ALL" in arguments
    assert "agent-eval-control" in arguments
    assert "OPENAI_API_KEY" not in command
    assert "--ephemeral" in arguments
    assert "--sandbox" not in command
    mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
    assert len(mounts) == 2
    assert any("dst=/workspace,readonly" in mount for mount in mounts)
    assert any("dst=/run/codex" in mount for mount in mounts)


def test_codex_permission_profile_and_jsonl_usage() -> None:
    config = _codex_config("evaluator")
    assert '":root" = "deny"' in config
    assert '":workspace_roots"' in config
    assert "enabled = false" in config
    assert 'inherit = "none"' in config
    usage = _usage(
        b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n'
    )
    assert usage == {
        "model_calls": 1,
        "tokens": 15,
        "input_tokens": 12,
        "output_tokens": 3,
    }


def test_codex_executor_transforms_solver_output_and_records_provenance(
    tmp_path: Path,
) -> None:
    class FakeAuth:
        def identity(self) -> SubscriptionAuthIdentity:
            return SubscriptionAuthIdentity("chatgpt_subscription", "codex-cli 0.153.1")

        def install_into(self, destination: Path) -> SubscriptionAuthIdentity:
            destination.mkdir()
            (destination / "auth.json").write_text('{"tokens":"fake"}', encoding="utf-8")
            return self.identity()

        @staticmethod
        def redact(payload: bytes) -> bytes:
            return payload

    class FakeRunner:
        def preflight(self, **_values: object) -> ContainerPreflight:
            return ContainerPreflight(
                "sha256:image",
                "network-id",
                "codex-cli 0.153.1",
                "27.5.1",
                "Python 3.12.8",
            )

        def run(self, arguments, **_values: object) -> ContainerOutcome:  # type: ignore[no-untyped-def]
            mount = next(
                arguments[index + 1]
                for index, value in enumerate(arguments)
                if value == "--mount" and "dst=/run/codex" in arguments[index + 1]
            )
            source = next(part.removeprefix("src=") for part in mount.split(",") if part.startswith("src="))
            assert 'model_reasoning_effort = "high"' in (
                Path(source) / "config.toml"
            ).read_text(encoding="utf-8")
            (Path(source) / "result.json").write_text(
                json.dumps({"response": "The isolated answer."}), encoding="utf-8"
            )
            stdout = (
                b'{"type":"turn.completed","usage":{"input_tokens":10,'
                b'"output_tokens":5}}\n'
            )
            return ContainerOutcome(0, stdout, b"")

    workspace_configuration = SimpleNamespace(
        stages={"solve": SimpleNamespace(model="solver")},
        models={"solver": SimpleNamespace(parameters={}, reasoning_effort="high")},
    )
    compiled = SimpleNamespace(
        repository=SimpleNamespace(workspace=workspace_configuration)
    )
    ledger = StageLedger(FilesystemArtifactStore(tmp_path / "external"))
    packet = _solve_packet(ledger, _case_input())
    output = CodexCliExecutor(
        compiled,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        _executor(),
        auth_store=FakeAuth(),  # type: ignore[arg-type]
        runner=FakeRunner(),  # type: ignore[arg-type]
    ).execute(packet, ledger)
    assert output.value["response"] == "The isolated answer."
    assert output.value["case_id"] == "example"
    assert output.value["program_hash"] == f"sha256:{'4' * 64}"
    assert output.usage["tokens"] == 15
    assert output.provenance["authentication"] == "chatgpt_subscription"
    assert output.provenance["python_version"] == "Python 3.12.8"
