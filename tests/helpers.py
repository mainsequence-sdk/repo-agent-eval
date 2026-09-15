from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import dspy
import yaml
from litellm import ModelResponse

from ms_agent_eval.core.evaluation import CaseDefinition
from ms_agent_eval.core.hashing import json_value, sha256_file
from ms_agent_eval.core.models import SnapshotLock
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.snapshots import ExternalSnapshotStore, SnapshotBuilder
from ms_agent_eval.core.sources import ResolvedSource
from ms_agent_eval.core.workspace import WorkspaceRepository


CASE_AUTHORING_INSTRUCTIONS = """# Test case-authoring policy

Author grounded cases from immutable instruction and source context.
Write schema-v6 packages and design rubrics for semantic evaluation by the LLM judge.
"""


def workspace_payload(*, skills: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": 17,
        "workspace": {"id": "synthetic-evaluation"},
        "recording": {
            "enabled": False,
            "recorder": "local-mlflow",
            "queue": "local-recorder-queue",
            "failure_policy": "continue",
            "wait_on_run_exit": False,
            "artifact_policy": "full",
            "content_policy": "full",
            "projection": {
                "live": {"enabled": True, "minimum_interval_seconds": 15},
                "stage_boundary": {"tables": "incremental", "summaries": True},
                "finalization": {"reports": True, "plotly": True, "evidence": True},
                "traces": {
                    "optimization": {
                        "policy": "winners_failures_and_sample",
                        "sample_rate": 0.05,
                    },
                    "validation": {"policy": "full"},
                    "test": {"policy": "full"},
                },
            },
        },
        "recorders": {
            "local-mlflow": {
                "type": "mlflow",
                "tracking_uri": "http://127.0.0.1:5000",
                "experiment_name": "ms-agent-eval/{workspace_id}",
                "authentication": {"type": "none"},
            }
        },
        "recorder_queues": {
            "local-recorder-queue": {
                "type": "filesystem",
                "path": "operations/recorder-queue",
                "live_coalescing": True,
                "lease_seconds": 300,
                "max_attempts": 12,
            }
        },
        "executors": {
            "remote-models": {
                "type": "endpoint",
                "adapter": "openai_compatible",
                "endpoints": [
                    {
                        "id": "primary",
                        "base_url": "http://localhost:8000/v1",
                        "max_concurrent_requests": 1,
                        "api_key_env": "TEST_API_KEY",
                        "routes": [
                            {"deployment": "solver-test", "model": "solver"},
                            {"deployment": "optimizer-test", "model": "optimizer"},
                            {"deployment": "judge-test", "model": "judge"},
                        ],
                    }
                ],
            },
            "authoring-harness": {
                "type": "harness",
                "adapter": "artifact_exchange",
            },
            "local-process": {"type": "process", "adapter": "local"},
        },
        "deployments": {
            "author-test": {
                "executor": "authoring-harness",
                "model": "author",
                "revision": "author-test-v1",
            },
            "solver-test": {
                "executor": "remote-models",
                "model": "solver",
                "revision": "solver-test-v1",
            },
            "optimizer-test": {
                "executor": "remote-models",
                "model": "optimizer",
                "revision": "optimizer-test-v1",
            },
            "judge-test": {
                "executor": "remote-models",
                "model": "judge",
                "revision": "judge-test-v1",
            },
        },
        "models": {
            "author": {
                "deployment": "author-test",
                "parameters": {"temperature": 0.0},
            },
            "solver": {
                "deployment": "solver-test",
                "parameters": {"temperature": 0.0},
            },
            "optimizer": {
                "deployment": "optimizer-test",
                "parameters": {"temperature": 0.0},
            },
            "judge": {
                "deployment": "judge-test",
                "parameters": {"temperature": 0.0},
            },
        },
        "stages": {
            "author": {
                "executor": "authoring-harness",
                "model": "author",
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 1, "tokens": 10000, "wall_seconds": 300},
            },
            "validate_judge": {
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 10, "tokens": 10000, "wall_seconds": 300},
            },
            "optimize": {
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 1, "tokens": 10000, "wall_seconds": 300},
            },
            "solve": {
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 10, "tokens": 10000, "wall_seconds": 300},
            },
            "judge": {
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 30, "tokens": 10000, "wall_seconds": 300},
            },
            "fit": {
                "executor": "local-process",
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 300},
            },
            "select": {
                "executor": "local-process",
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 300},
            },
            "aggregate": {
                "executor": "local-process",
                "reuse": {"policy": "exact"},
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 300},
            },
        },
        "evaluation": {
            "repository": {
                "url": "https://github.com/example/synthetic",
                "ref": "v1.0.0",
            },
            "instructions": {
                "global": ["AGENTS.md"],
                "skills": dict({"directory": "skills"} if skills is None else skills),
            },
            "case_authoring": {
                "instructions": "case-authoring.md",
            },
            "cases": {
                "directory": "cases",
                "coverage": {
                    "catalog": "cases/coverage.yaml",
                    "minimum_cases_per_skill": 1,
                    "minimum_leakage_groups_per_skill": 1,
                },
            },
            "splits": {"file": "cases/splits.yaml"},
            "judge": {
                "dspy": {
                    "module": "Predict",
                    "signature": {
                        "inputs": {
                            "task": "str",
                            "skill_context": "str",
                            "rubric": "str",
                            "expected_response": "str",
                            "expected_artifacts": "str",
                            "candidate_response": "str",
                        },
                        "outputs": {
                            "criterion_scores": "dict[str, float]",
                            "hard_failures": "list[str]",
                            "feedback": "str",
                        },
                    },
                },
                "validation": {"directory": "judge-validation"},
                "repetitions": 1,
            },
        },
        "experiments": {
            "baseline": {
                "mode": "evaluate",
                "roles": {"solver": "solver", "judge": "judge"},
                "solver": {
                    "dspy": {
                        "module": "Predict",
                        "signature": {
                            "inputs": {
                                "global_context": "str",
                                "skill_context": "str",
                                "task": "str",
                            },
                            "outputs": {"response": "str"},
                        },
                    },
                },
                "runtime": {"type": "response_only", "python": "3.12"},
                "repetitions": 1,
            },
            "optimize": {
                "mode": "optimize",
                "roles": {"solver": "solver", "judge": "judge"},
                "based_on": "baseline",
                "optimizer": {
                    "strategy": "labeled_few_shot",
                    "search": {"k": [1]},
                },
                "validation": {"strategy": "holdout", "fraction": 0.5, "seed": 0},
                "budget": {
                    "solver": {"model_calls": 20, "tokens": 10000},
                    "judge": {"model_calls": 30, "tokens": 10000},
                    "wall_seconds": 300,
                },
            },
        },
    }


def create_workspace(root: Path) -> WorkspaceRepository:
    root.mkdir(parents=True)
    payload = workspace_payload()
    payload["workspace"]["data_root"] = (root.parent / "runtime").as_posix()  # type: ignore[index]
    (root / "workspace.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    (root / "case-authoring.md").write_text(CASE_AUTHORING_INSTRUCTIONS, encoding="utf-8")
    (root / "cases").mkdir()
    (root / "cases" / "coverage.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "status": "complete",
                "default_minimum_primary_cases": 1,
                "skills": {
                    skill: {
                        "capabilities": [
                            {
                                "id": "correctness",
                                "title": "Correctness",
                                "description": f"Apply the {skill} skill correctly.",
                            }
                        ]
                    }
                    for skill in ("alpha", "beta", "gamma")
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "judge-validation").mkdir()
    return WorkspaceRepository.from_file(root / "workspace.yaml")


def create_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    for skill in ("alpha", "beta", "gamma"):
        path = checkout / "skills" / skill
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"# {skill}\n\nUse {skill}.\n", encoding="utf-8")
    (checkout / "AGENTS.md").write_text("# Global\n\nBe precise.\n", encoding="utf-8")
    return checkout


def create_snapshot(
    repository: WorkspaceRepository, checkout: Path, data_root: Path
) -> tuple[SnapshotLock, Path]:
    source = repository.target_specification().source
    resolved = ResolvedSource(
        source.repository_url,
        source.repository_url + ".git",
        source.ref,
        "a" * 40,
    )
    store = ExternalSnapshotStore(data_root, workspace_root=repository.root)
    lock = SnapshotBuilder(None, store).create_from_checkout(  # type: ignore[arg-type]
        repository.target_specification(), resolved, checkout
    )
    return lock, store.directory(lock)


def create_case(
    cases_root: Path,
    *,
    case_id: str,
    skill: str,
    group: str,
    source_path: str,
    authoring_instructions_hash: str = f"sha256:{'1' * 64}",
    source_snapshot_hash: str = f"sha256:{'3' * 64}",
    capability_catalog_hash: str | None = None,
) -> CaseDefinition:
    directory = cases_root / skill / case_id
    solver = directory / "solver"
    expected = directory / "judge" / "expected"
    expected.mkdir(parents=True)
    solver.mkdir(parents=True)
    (solver / "prompt.md").write_text(f"Solve {case_id}.\n", encoding="utf-8")
    (expected / "response.md").write_text(f"Correct {case_id}.\n", encoding="utf-8")
    (directory / "judge" / "rubric.yaml").write_text(
        yaml.safe_dump(
            {
                "criteria": [
                    {
                        "id": "correctness",
                        "weight": 1.0,
                        "description": "The response is correct.",
                    }
                ],
                "hard_failures": [{"id": "unsafe", "description": "The response is unsafe."}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 6,
        "id": case_id,
        "title": f"Case {case_id}",
        "skill": skill,
        "group": group,
        "coverage": {
            "primary": "correctness",
            "supporting": [],
            "archetype": "construction",
        },
        "solver": {"prompt": "solver/prompt.md"},
        "judge": {
            "expected_response": "judge/expected/response.md",
            "rubric": "judge/rubric.yaml",
        },
        "source_paths": [source_path],
        "provenance": {
            "authoring_agent": "codex",
            "authoring_instructions_hash": authoring_instructions_hash,
            "capability_catalog_hash": (
                capability_catalog_hash or sha256_file(cases_root / "coverage.yaml")
            ),
            "source_snapshot_hash": source_snapshot_hash,
            "case_content_hash": "pending",
        },
    }
    case_file = directory / "case.yaml"
    case_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    package_hash = CaseDefinition.load(directory, verify_provenance=False).content_hash
    payload["provenance"]["case_content_hash"] = package_hash  # type: ignore[index]
    case_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return CaseDefinition.load(directory)


def seed_cases_and_judge_validation(
    repository: WorkspaceRepository,
    *,
    authoring_instructions_hash: str = f"sha256:{'1' * 64}",
    source_snapshot_hash: str = f"sha256:{'3' * 64}",
) -> None:
    cases = [
        create_case(
            repository.cases_root,
            case_id="case-train",
            skill="alpha",
            group="group-0",
            source_path="skills/alpha/SKILL.md",
            authoring_instructions_hash=authoring_instructions_hash,
            source_snapshot_hash=source_snapshot_hash,
        ),
        create_case(
            repository.cases_root,
            case_id="case-train-two",
            skill="beta",
            group="group-17",
            source_path="skills/beta/SKILL.md",
            authoring_instructions_hash=authoring_instructions_hash,
            source_snapshot_hash=source_snapshot_hash,
        ),
        create_case(
            repository.cases_root,
            case_id="case-test",
            skill="gamma",
            group="group-1",
            source_path="skills/gamma/SKILL.md",
            authoring_instructions_hash=authoring_instructions_hash,
            source_snapshot_hash=source_snapshot_hash,
        ),
    ]
    (repository.cases_root / "splits.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "groups": {
                    "group-0": "train",
                    "group-17": "train",
                    "group-1": "test",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    fixtures = []
    for label in ("strong", "partial", "incorrect", "contradictory", "adversarial"):
        candidate = repository.judge_validation_root / f"{label}.md"
        candidate.write_text(f"{label} candidate\n", encoding="utf-8")
        fixtures.append(
            {
                "id": label,
                "case": cases[0].id,
                "candidate": candidate.name,
                "label": label,
            }
        )
    (repository.judge_validation_root / "manifest.yaml").write_text(
        yaml.safe_dump({"schema_version": 3, "fixtures": fixtures}, sort_keys=False),
        encoding="utf-8",
    )


class FixedObservedLM(dspy.BaseLM):
    def __init__(
        self,
        observer: ModelCallObserver,
        responses: Sequence[str],
        *,
        model: str,
    ) -> None:
        super().__init__(model=model, cache=False)
        self.observer = observer
        self.responses = list(responses)

    def forward(self, prompt=None, messages=None, **kwargs):  # type: ignore[no-untyped-def]
        if not self.responses:
            raise AssertionError(f"no fixed response remains for {self.model}")
        output = self.responses.pop(0)
        rendered = messages or [{"role": "user", "content": prompt}]
        started = time.monotonic()
        response = ModelResponse(
            model=self.model,
            choices=[{"message": {"role": "assistant", "content": output}}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )
        self.observer.completed(
            provider_id="fixed",
            model=self.model,
            parameters={},
            messages=rendered,
            request={"messages": rendered},
            response=json_value(response.model_dump()),  # type: ignore[arg-type]
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            latency_seconds=time.monotonic() - started,
            configured_cost=0.0,
        )
        return response


def solver_response(value: str = "correct") -> str:
    return f"[[ ## response ## ]]\n{value}\n\n[[ ## completed ## ]]"


def judge_response(score: float) -> str:
    return (
        "[[ ## criterion_scores ## ]]\n"
        f'{{"correctness": {score}}}\n\n'
        "[[ ## hard_failures ## ]]\n[]\n\n"
        "[[ ## feedback ## ]]\nGrounded feedback.\n\n"
        "[[ ## completed ## ]]"
    )
