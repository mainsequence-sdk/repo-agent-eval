from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass

import dspy

from ms_agent_eval.programs.dspy.engine import (
    create_copro_proposal_program,
    create_judge_program,
    create_solver_program,
    solver_prompt_state,
)

from .errors import ConfigurationError
from .evaluation import CaseDefinition
from .hashing import content_hash
from .planning import CompiledWorkspace
from .token_preflight import EndpointTokenPreflight, TokenPreflightResult
from .workload import calculate_workload
from .workspace import (
    EndpointConfiguration,
    ExecutorConfiguration,
    ExecutorType,
    ExperimentMode,
    CoproConfiguration,
    ResolvedRoleModel,
)


_SUPPORTED_STAGES = ("validate_judge", "optimize", "solve", "judge")


@dataclass(frozen=True)
class _RenderedRequest:
    id: str
    stage: str
    case_id: str
    messages: tuple[Mapping[str, object], ...]
    multiplicity: int


@dataclass(frozen=True)
class _CheckedRequest:
    request: _RenderedRequest
    endpoint_results: Mapping[str, TokenPreflightResult]


def calculate_preflight(
    workspace: CompiledWorkspace,
    experiment_id: str,
    *,
    environment: Mapping[str, str],
    stages: Sequence[str] = _SUPPORTED_STAGES,
    case_ids: Sequence[str] = (),
    pilot: bool = False,
    token_preflight: EndpointTokenPreflight | None = None,
) -> Mapping[str, object]:
    """Calculate endpoint-native token fit without making generation calls."""

    selected_stages = _normalize_stages(stages)
    lock = workspace.experiment_lock(experiment_id, environment=environment)
    cases = _select_cases(workspace, case_ids=case_ids, pilot=pilot)
    workload = calculate_workload(
        workspace,
        experiment_id,
        environment=environment,
        pilot=pilot,
    )
    checker = token_preflight or EndpointTokenPreflight()
    reports: list[Mapping[str, object]] = []

    for stage in selected_stages:
        role = "solver" if stage == "solve" else ("optimizer" if stage == "optimize" else "judge")
        if stage == "optimize" and not isinstance(lock.configuration.optimizer, CoproConfiguration):
            reports.append(
                _deferred_stage_report(
                    stage,
                    role,
                    lock.models["solver"],
                    workspace.repository.workspace.executors[lock.models["solver"].executor_id],
                    request_count=0,
                    reason="strategy_does_not_use_optimizer_calls",
                    detail="labeled_few_shot compiles demonstrations locally.",
                    state="not_applicable",
                )
            )
            continue
        model = lock.models[role]
        executor = workspace.repository.workspace.executors[model.executor_id]

        if stage == "judge":
            reports.append(
                _deferred_stage_report(
                    stage,
                    role,
                    model,
                    executor,
                    request_count=_judge_request_count(
                        workspace,
                        lock.configuration.repetitions,
                        cases,
                        pilot=pilot,
                    ),
                    reason="solver_responses_required",
                    detail=(
                        "Judge messages contain solver responses, so their exact token shape "
                        "does not exist before solve completes."
                    ),
                )
            )
            continue

        if executor.type is not ExecutorType.ENDPOINT:
            reports.append(
                _deferred_stage_report(
                    stage,
                    role,
                    model,
                    executor,
                    request_count=0,
                    reason="endpoint_preflight_not_applicable",
                    detail="The stage does not use an endpoint executor.",
                    state="not_applicable",
                )
            )
            continue

        if stage == "solve" and lock.configuration.mode is ExperimentMode.OPTIMIZE:
            strategy = lock.configuration.optimizer.strategy  # type: ignore[union-attr]
            reports.append(
                _deferred_stage_report(
                    stage,
                    role,
                    model,
                    executor,
                    request_count=0,
                    reason="compiled_program_required",
                    detail=(
                        f"The {strategy} strategy changes the compiled solver state. Exact "
                        "messages can be calculated only after that state exists."
                    ),
                )
            )
            continue

        requests = (
            _judge_validation_requests(workspace, pilot=pilot)
            if stage == "validate_judge"
            else (
                _optimizer_requests(
                    workspace,
                    lock.configuration.optimizer,
                )
                if stage == "optimize"
                else _solver_requests(
                    workspace,
                    cases,
                    repetitions=_solver_repetitions(
                        workspace,
                        experiment_repetitions=lock.configuration.repetitions,
                        pilot=pilot,
                    ),
                )
            )
        )
        reports.append(
            _check_stage(
                stage,
                role,
                requests,
                model,
                executor,
                environment=environment,
                token_preflight=checker,
            )
        )

    exact = [item for item in reports if item["state"] == "calculated"]
    blocked = sum(int(item["blocked_requests"]) for item in exact)
    unknown = sum(int(item["unknown_only_requests"]) for item in exact)
    if blocked:
        status = "blocked"
    elif unknown:
        status = "unknown"
    elif exact:
        status = "fits"
    else:
        status = "deferred"
    return {
        "schema_version": 1,
        "status": status,
        "generation_calls": 0,
        "experiment": experiment_id,
        "experiment_revision_id": lock.content_hash,
        "execution_mode": "pilot" if pilot else "benchmark",
        "selected_case_count": len(cases),
        "selected_case_ids": [case.id for case in cases],
        "workload": workload,
        "optimization_plan": workspace.validation.get("optimization", {}).get(experiment_id),
        "stages": reports,
        "summary": {
            "calculated_stages": len(exact),
            "deferred_stages": sum(item["state"] == "deferred" for item in reports),
            "blocked_requests": blocked,
            "unknown_only_requests": unknown,
            "all_calculated_requests_have_exact_fit": bool(exact) and blocked == 0 and unknown == 0,
        },
    }


def _normalize_stages(stages: Sequence[str]) -> tuple[str, ...]:
    expanded = _SUPPORTED_STAGES if not stages or "all" in stages else tuple(stages)
    unknown = sorted(set(expanded) - set(_SUPPORTED_STAGES))
    if unknown:
        raise ConfigurationError(f"unsupported preflight stages: {unknown}")
    return tuple(stage for stage in _SUPPORTED_STAGES if stage in set(expanded))


def _select_cases(
    workspace: CompiledWorkspace,
    *,
    case_ids: Sequence[str],
    pilot: bool,
) -> tuple[CaseDefinition, ...]:
    known = {case.id: case for case in workspace.case_bank.cases}
    unknown = sorted(set(case_ids) - set(known))
    if unknown:
        raise ConfigurationError(f"unknown preflight case ids: {unknown}")
    selected_split = "train" if pilot else "test"
    return tuple(
        case
        for case in workspace.case_bank.cases
        if (not case_ids and workspace.splits.split_for(case) == selected_split)
        or (case_ids and case.id in set(case_ids))
    )


def _adapter_messages(
    program: dspy.Predict,
    inputs: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    adapter = dspy.ChatAdapter(use_json_adapter_fallback=False)
    rendered = adapter.format(program.signature, program.demos, dict(inputs))
    return tuple({str(key): value for key, value in message.items()} for message in rendered)


def _solver_requests(
    workspace: CompiledWorkspace,
    cases: Sequence[CaseDefinition],
    *,
    repetitions: int,
) -> tuple[_RenderedRequest, ...]:
    program = create_solver_program()
    return tuple(
        _RenderedRequest(
            id=f"solve:{case.id}",
            stage="solve",
            case_id=case.id,
            messages=_adapter_messages(
                program,
                {
                    "global_context": workspace.context.global_context,
                    "skill_context": workspace.context.skill(case.skill),
                    "task": case.prompt,
                },
            ),
            multiplicity=repetitions,
        )
        for case in cases
    )


def _optimizer_requests(
    workspace: CompiledWorkspace,
    optimizer: object,
) -> tuple[_RenderedRequest, ...]:
    if not isinstance(optimizer, CoproConfiguration):
        return ()
    program = create_copro_proposal_program()
    instruction, prefix = solver_prompt_state(create_solver_program())
    skills = (
        sorted(workspace.context.skill_contexts)
        if optimizer.skills == "all"
        else list(optimizer.skills)
    )
    targets = []
    if optimizer.optimize_prompt:
        targets.append(("solver_prompt", "solver", instruction, prefix))
    targets.extend(("skill", skill, workspace.context.skill(skill), "") for skill in skills)
    multiplicity = optimizer.depth * optimizer.partitions
    return tuple(
        _RenderedRequest(
            id=f"optimize:{kind}:{target_id}",
            stage="optimize",
            case_id=target_id,
            messages=_adapter_messages(
                program,
                {
                    "target_kind": kind,
                    "target_id": target_id,
                    "current_text": text,
                    "current_output_prefix": output_prefix,
                    "scored_attempts": "[]",
                    "requirements": "Preserve the locked contract and technical facts.",
                    "candidate_count": optimizer.breadth - 1,
                },
            ),
            multiplicity=multiplicity,
        )
        for kind, target_id, text, output_prefix in targets
    )


def _judge_validation_requests(
    workspace: CompiledWorkspace,
    *,
    pilot: bool,
) -> tuple[_RenderedRequest, ...]:
    program = create_judge_program()
    repetitions = 1 if pilot else workspace.repository.workspace.evaluation.judge.repetitions
    cases = {case.id: case for case in workspace.case_bank.cases}
    requests = []
    for fixture in workspace.judge_validation.fixtures:
        case = cases[fixture.case_id]
        requests.append(
            _RenderedRequest(
                id=f"validate_judge:{fixture.id}",
                stage="validate_judge",
                case_id=case.id,
                messages=_adapter_messages(
                    program,
                    {
                        "task": case.prompt,
                        "skill_context": workspace.context.skill(case.skill),
                        "rubric": case.rubric.prompt_text(),
                        "expected_response": case.expected_response,
                        "expected_artifacts": json.dumps(
                            case.expected_artifacts(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "candidate_response": fixture.candidate_path.read_text(encoding="utf-8"),
                    },
                ),
                multiplicity=repetitions,
            )
        )
    return tuple(requests)


def _solver_repetitions(
    workspace: CompiledWorkspace,
    *,
    experiment_repetitions: int,
    pilot: bool,
) -> int:
    if pilot:
        return 1
    reuse = workspace.repository.workspace.stages["solve"].reuse
    return reuse.samples if reuse.policy.value == "replicate" else experiment_repetitions


def _judge_request_count(
    workspace: CompiledWorkspace,
    experiment_repetitions: int,
    cases: Sequence[CaseDefinition],
    *,
    pilot: bool,
) -> int:
    solver_repetitions = _solver_repetitions(
        workspace,
        experiment_repetitions=experiment_repetitions,
        pilot=pilot,
    )
    judge_repetitions = 1 if pilot else workspace.repository.workspace.evaluation.judge.repetitions
    return len(cases) * solver_repetitions * judge_repetitions


def _maximum_output_tokens(model: ResolvedRoleModel) -> int | None:
    value = model.parameters.get("max_tokens")
    if value is None:
        value = model.parameters.get("max_completion_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_stage(
    stage: str,
    role: str,
    requests: Sequence[_RenderedRequest],
    model: ResolvedRoleModel,
    executor: ExecutorConfiguration,
    *,
    environment: Mapping[str, str],
    token_preflight: EndpointTokenPreflight,
) -> Mapping[str, object]:
    endpoints = model.eligible_endpoints(executor)
    results: dict[tuple[str, str], TokenPreflightResult] = {}
    with ExitStack() as stack:
        endpoint_pools = {
            endpoint.id: stack.enter_context(
                ThreadPoolExecutor(
                    max_workers=endpoint.max_concurrent_requests,
                    thread_name_prefix=f"preflight-{stage}-{endpoint.id}",
                )
            )
            for endpoint in endpoints
        }
        futures = {
            endpoint_pools[endpoint.id].submit(
                token_preflight.evaluate,
                endpoint,
                model=model.bind_endpoint(endpoint).model,
                deployment_revision=model.deployment_revision,
                messages=request.messages,
                maximum_output_tokens=_maximum_output_tokens(model),
                reasoning_effort=model.reasoning_effort,
                api_key=endpoint.api_key(environment),
            ): (request.id, endpoint.id)
            for request in requests
            for endpoint in endpoints
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    checked = tuple(
        _CheckedRequest(
            request,
            {endpoint.id: results[(request.id, endpoint.id)] for endpoint in endpoints},
        )
        for request in requests
    )
    details = tuple(_request_report(item, executor) for item in checked)
    logical_count = sum(request.multiplicity for request in requests)
    blocked = sum(
        int(detail["dispatchable_capacity"] == 0) * request.multiplicity
        for request, detail in zip(requests, details, strict=True)
    )
    unknown_only = sum(
        int(detail["guaranteed_fit_capacity"] == 0 and detail["dispatchable_capacity"] > 0)
        * request.multiplicity
        for request, detail in zip(requests, details, strict=True)
    )
    guaranteed_parallel = _parallel_capacity(
        checked,
        executor,
        allowed_statuses={"fits"},
    )
    dispatchable_parallel = _parallel_capacity(
        checked,
        executor,
        allowed_statuses={"fits", "skipped"},
    )
    endpoint_reports = tuple(_endpoint_report(endpoint, checked) for endpoint in endpoints)
    configured_capacity = sum(endpoint.max_concurrent_requests for endpoint in endpoints)
    state = "calculated"
    status = "blocked" if blocked else ("unknown" if unknown_only else "fits")
    return {
        "stage": stage,
        "role": role,
        "state": state,
        "status": status,
        "model": model.model,
        "deployment_id": model.deployment_id,
        "deployment_revision": model.deployment_revision,
        "reasoning_effort": model.reasoning_effort,
        "executor": executor.id,
        "configured_capacity": configured_capacity,
        "logical_request_count": logical_count,
        "unique_rendered_request_count": len(requests),
        "maximum_output_tokens": _maximum_output_tokens(model),
        "guaranteed_parallel_capacity": guaranteed_parallel,
        "dispatchable_parallel_capacity": dispatchable_parallel,
        "minimum_dispatch_waves": _minimum_waves(
            checked,
            executor,
            allowed_statuses={"fits", "skipped"},
        ),
        "blocked_requests": blocked,
        "unknown_only_requests": unknown_only,
        "endpoints": endpoint_reports,
        "requests": details,
    }


def _request_report(
    checked: _CheckedRequest,
    executor: ExecutorConfiguration,
) -> Mapping[str, object]:
    outcomes = []
    guaranteed = 0
    dispatchable = 0
    for endpoint in executor.endpoints:
        if endpoint.id not in checked.endpoint_results:
            continue
        result = checked.endpoint_results[endpoint.id]
        if result.status == "fits":
            guaranteed += endpoint.max_concurrent_requests
        if result.permits_dispatch:
            dispatchable += endpoint.max_concurrent_requests
        outcomes.append(
            {
                "endpoint": endpoint.id,
                "capacity": endpoint.max_concurrent_requests,
                **result.as_dict(),
            }
        )
    return {
        "id": checked.request.id,
        "case_id": checked.request.case_id,
        "multiplicity": checked.request.multiplicity,
        "rendered_message_hash": content_hash(checked.request.messages),
        "guaranteed_fit_capacity": guaranteed,
        "dispatchable_capacity": dispatchable,
        "endpoints": outcomes,
    }


def _endpoint_report(
    endpoint: EndpointConfiguration,
    requests: Sequence[_CheckedRequest],
) -> Mapping[str, object]:
    weighted_results = [
        (item.endpoint_results[endpoint.id], item.request.multiplicity) for item in requests
    ]
    results = [result for result, _ in weighted_results]
    statuses = Counter()
    reasons = Counter()
    for result, multiplicity in weighted_results:
        statuses[result.status] += multiplicity
        reasons[result.reason] += multiplicity
    known_required = [
        result.required_tokens for result in results if result.required_tokens is not None
    ]
    contexts = {result.context_window for result in results if result.context_window is not None}
    return {
        "id": endpoint.id,
        "base_url": endpoint.base_url,
        "capacity": endpoint.max_concurrent_requests,
        "context_window": next(iter(contexts)) if len(contexts) == 1 else None,
        "fits": statuses["fits"],
        "exceeds": statuses["exceeds"],
        "skipped": statuses["skipped"],
        "minimum_required_tokens": min(known_required) if known_required else None,
        "maximum_required_tokens": max(known_required) if known_required else None,
        "reasons": dict(sorted(reasons.items())),
    }


def _parallel_capacity(
    requests: Sequence[_CheckedRequest],
    executor: ExecutorConfiguration,
    *,
    allowed_statuses: set[str],
) -> int:
    """Maximum simultaneous requests via a small deterministic bipartite match."""

    return _matched_request_count(
        requests,
        executor,
        allowed_statuses=allowed_statuses,
        capacity_multiplier=1,
    )


def _minimum_waves(
    requests: Sequence[_CheckedRequest],
    executor: ExecutorConfiguration,
    *,
    allowed_statuses: set[str],
) -> int | None:
    logical_count = sum(item.request.multiplicity for item in requests)
    if logical_count == 0:
        return 0
    endpoint_ids = set(requests[0].endpoint_results) if requests else set()
    if any(
        not any(
            item.endpoint_results[endpoint.id].status in allowed_statuses
            for endpoint in executor.endpoints
            if endpoint.id in endpoint_ids
        )
        for item in requests
    ):
        return None
    lower = 1
    upper = logical_count
    while lower < upper:
        candidate = (lower + upper) // 2
        matched = _matched_request_count(
            requests,
            executor,
            allowed_statuses=allowed_statuses,
            capacity_multiplier=candidate,
        )
        if matched == logical_count:
            upper = candidate
        else:
            lower = candidate + 1
    return lower


def _matched_request_count(
    requests: Sequence[_CheckedRequest],
    executor: ExecutorConfiguration,
    *,
    allowed_statuses: set[str],
    capacity_multiplier: int,
) -> int:

    endpoint_ids = set(requests[0].endpoint_results) if requests else set()
    slots = [
        endpoint.id
        for endpoint in executor.endpoints
        if endpoint.id in endpoint_ids
        for _ in range(endpoint.max_concurrent_requests * capacity_multiplier)
    ]
    logical_requests = [item for item in requests for _ in range(item.request.multiplicity)]
    slot_owner: dict[int, int] = {}

    def assign(request_index: int, visited: set[int]) -> bool:
        request = logical_requests[request_index]
        for slot_index, endpoint_id in enumerate(slots):
            if slot_index in visited:
                continue
            if request.endpoint_results[endpoint_id].status not in allowed_statuses:
                continue
            visited.add(slot_index)
            owner = slot_owner.get(slot_index)
            if owner is None or assign(owner, visited):
                slot_owner[slot_index] = request_index
                return True
        return False

    matched = 0
    for request_index in range(len(logical_requests)):
        if assign(request_index, set()):
            matched += 1
        if matched == len(slots):
            break
    return matched


def _deferred_stage_report(
    stage: str,
    role: str,
    model: ResolvedRoleModel,
    executor: ExecutorConfiguration,
    *,
    request_count: int,
    reason: str,
    detail: str,
    state: str = "deferred",
) -> Mapping[str, object]:
    return {
        "stage": stage,
        "role": role,
        "state": state,
        "status": state,
        "model": model.model,
        "deployment_id": model.deployment_id,
        "deployment_revision": model.deployment_revision,
        "reasoning_effort": model.reasoning_effort,
        "executor": executor.id,
        "configured_capacity": sum(
            endpoint.max_concurrent_requests for endpoint in model.eligible_endpoints(executor)
        ),
        "logical_request_count": request_count,
        "unique_rendered_request_count": 0,
        "guaranteed_parallel_capacity": 0,
        "dispatchable_parallel_capacity": 0,
        "minimum_dispatch_waves": None,
        "blocked_requests": 0,
        "unknown_only_requests": 0,
        "reason": reason,
        "detail": detail,
        "endpoints": [],
        "requests": [],
    }


__all__ = ["calculate_preflight"]
