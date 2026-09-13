from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from ms_agent_eval.core.hashing import content_hash, sha256_file
from ms_agent_eval.core.planning import compile_workspace
from ms_agent_eval.core.preflight_diagnostics import calculate_preflight
from ms_agent_eval.core.token_preflight import TokenPreflightResult
from ms_agent_eval.core.workspace import EndpointConfiguration, WorkspaceRepository

from .helpers import (
    create_checkout,
    create_snapshot,
    create_workspace,
    seed_cases_and_judge_validation,
)


class _FixedPreflight:
    def __init__(self, statuses: Mapping[str, str]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, Sequence[Mapping[str, object]]]] = []

    def evaluate(
        self,
        endpoint: EndpointConfiguration,
        *,
        model: str,
        deployment_revision: str,
        messages: Sequence[Mapping[str, object]],
        maximum_output_tokens: int | None,
        reasoning_effort: str | None,
        api_key: str | None,
    ) -> TokenPreflightResult:
        del model, deployment_revision, reasoning_effort
        assert api_key == "test-secret"
        self.calls.append((endpoint.id, messages))
        status = self.statuses[endpoint.id]
        context = 200 if status != "skipped" else None
        input_tokens = 20 if status != "skipped" else None
        required = (
            None
            if input_tokens is None or maximum_output_tokens is None
            else input_tokens + maximum_output_tokens
        )
        return TokenPreflightResult(
            status=status,  # type: ignore[arg-type]
            reason=(
                "exact_endpoint_tokenization"
                if status != "skipped"
                else "tokenize_unsupported"
            ),
            context_window=context,
            input_tokens=input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            required_tokens=required,
            tokenizer_fingerprint="sha256:tokenizer" if context else None,
            chat_template_fingerprint="sha256:template" if context else None,
            capability_source="props" if context else None,
            capability_checked_at="2026-09-14T00:00:00+00:00" if context else None,
            rendered_message_hash=content_hash(list(messages)),
        )


def _compiled(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = create_workspace(tmp_path / "workspace")
    payload = yaml.safe_load(repository.workspace_file.read_text(encoding="utf-8"))
    payload["executors"]["remote-models"]["endpoints"] = [
        {
            "id": "primary",
            "base_url": "http://localhost:8000/v1",
            "max_concurrent_requests": 2,
            "api_key_env": "TEST_API_KEY",
        },
        {
            "id": "secondary",
            "base_url": "http://localhost:8001/v1",
            "max_concurrent_requests": 1,
            "api_key_env": "TEST_API_KEY",
        },
    ]
    payload["models"]["solver"]["parameters"]["max_tokens"] = 100
    payload["models"]["judge"]["parameters"]["max_tokens"] = 50
    payload["experiments"]["baseline"]["repetitions"] = 3
    repository.workspace_file.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    repository = WorkspaceRepository.from_file(repository.workspace_file)
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


def test_preflight_calculates_exact_request_fit_and_usable_concurrency(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    checker = _FixedPreflight({"primary": "fits", "secondary": "skipped"})

    report = calculate_preflight(
        compiled,
        "baseline",
        environment={"TEST_API_KEY": "test-secret"},
        token_preflight=checker,  # type: ignore[arg-type]
    )

    assert report["status"] == "fits"
    assert report["generation_calls"] == 0
    stages = {item["stage"]: item for item in report["stages"]}
    validation = stages["validate_judge"]
    assert validation["logical_request_count"] == 5
    assert validation["unique_rendered_request_count"] == 5
    assert validation["configured_capacity"] == 3
    assert validation["guaranteed_parallel_capacity"] == 2
    assert validation["dispatchable_parallel_capacity"] == 3
    solve = stages["solve"]
    assert solve["logical_request_count"] == 3
    assert solve["unique_rendered_request_count"] == 1
    assert solve["maximum_output_tokens"] == 100
    assert solve["guaranteed_parallel_capacity"] == 2
    assert solve["dispatchable_parallel_capacity"] == 3
    assert solve["minimum_dispatch_waves"] == 1
    assert stages["judge"]["state"] == "deferred"
    assert stages["judge"]["reason"] == "solver_responses_required"
    assert len(checker.calls) == 12
    assert any("global_context" in str(messages) for _, messages in checker.calls)


def test_preflight_is_blocked_when_every_endpoint_rejects_a_request(
    tmp_path: Path,
) -> None:
    compiled = _compiled(tmp_path)
    checker = _FixedPreflight({"primary": "exceeds", "secondary": "exceeds"})

    report = calculate_preflight(
        compiled,
        "baseline",
        environment={"TEST_API_KEY": "test-secret"},
        stages=("solve",),
        token_preflight=checker,  # type: ignore[arg-type]
    )

    assert report["status"] == "blocked"
    solve = report["stages"][0]
    assert solve["blocked_requests"] == 3
    assert solve["guaranteed_parallel_capacity"] == 0
    assert solve["dispatchable_parallel_capacity"] == 0
    assert solve["minimum_dispatch_waves"] is None
