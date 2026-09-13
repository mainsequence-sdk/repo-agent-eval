from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

from .errors import ConfigurationError, IntegrityError


_SIGNATURES: Mapping[str, Mapping[str, Mapping[str, str]]] = {
    "solver": {
        "inputs": {
            "global_context": "str",
            "skill_context": "str",
            "task": "str",
        },
        "outputs": {"response": "str"},
    },
    "judge": {
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
}


@dataclass(frozen=True)
class WorkspaceTemplate:
    payload: Mapping[str, object]
    cases_directory: str
    api_key_env: str | None
    case_authoring_instructions: str
    capability_catalog: str
    case_bank_readme: str

    @property
    def workspace_yaml(self) -> str:
        return yaml.safe_dump(dict(self.payload), sort_keys=False)

    @property
    def env_example(self) -> str | None:
        if self.api_key_env is None:
            return None
        return f"# Optional local secret; never commit .env.\n{self.api_key_env}=\n"


def _program(role: str) -> Mapping[str, object]:
    signature = _SIGNATURES[role]
    return {
        "module": "Predict",
        "signature": {
            "inputs": dict(signature["inputs"]),
            "outputs": dict(signature["outputs"]),
        },
    }


def _model(
    *,
    name: str,
    temperature: float,
    reasoning_effort: str | None = None,
) -> Mapping[str, object]:
    return {
        "name": name,
        **(
            {"reasoning_effort": reasoning_effort}
            if reasoning_effort is not None
            else {}
        ),
        "parameters": {"temperature": temperature},
    }


def build_workspace_template(
    *,
    workspace_id: str,
    repository_url: str,
    repository_ref: str,
    global_instructions: Sequence[str],
    skills: Mapping[str, object],
    cases_directory: str,
    api_base_url: str,
    solver_model: str,
    judge_model: str,
    solver_reasoning_effort: str | None = None,
    judge_reasoning_effort: str | None = None,
    author_model: str = "gpt-5.6-sol",
    data_root: str | None = None,
    api_key_env: str | None = None,
    runtime_type: str = "response_only",
    runtime_python: str = "3.12",
    runtime_image: str | None = None,
    runtime_network: str = "none",
    baseline_repetitions: int = 1,
    judge_repetitions: int = 3,
    include_optimization: bool = True,
) -> WorkspaceTemplate:
    runtime: dict[str, object] = {
        "type": runtime_type,
        "python": runtime_python,
    }
    if runtime_type == "docker":
        runtime.update({"image": runtime_image, "network": runtime_network})
    workspace: dict[str, object] = {"id": workspace_id}
    if data_root is not None:
        workspace["data_root"] = data_root
    payload: dict[str, object] = {
        "schema_version": 8,
        "workspace": workspace,
        "executors": {
            "remote-models": {
                "type": "endpoint",
                "adapter": "openai_compatible",
                "base_url": api_base_url,
                **({"api_key_env": api_key_env} if api_key_env is not None else {}),
            },
            "authoring-harness": {
                "type": "harness",
                "adapter": "artifact_exchange",
            },
            "local-process": {"type": "process", "adapter": "local"},
        },
        "models": {
            "author": _model(name=author_model, temperature=0.0),
            "solver": _model(
                name=solver_model,
                temperature=0.2,
                reasoning_effort=solver_reasoning_effort,
            ),
            "judge": _model(
                name=judge_model,
                temperature=0.0,
                reasoning_effort=judge_reasoning_effort,
            ),
        },
        "stages": {
            "author": {
                "executor": "authoring-harness",
                "model": "author",
                "budget": {"model_calls": 1, "tokens": 64000, "wall_seconds": 1800},
            },
            "calibrate_judge": {
                "executor": "remote-models",
                "model": "judge",
                "budget": {"model_calls": 100, "tokens": 250000, "wall_seconds": 1800},
            },
            "solve": {
                "executor": "remote-models",
                "model": "solver",
                "budget": {"model_calls": 1, "tokens": 32000, "wall_seconds": 600},
            },
            "judge": {
                "executor": "remote-models",
                "model": "judge",
                "budget": {
                    "model_calls": judge_repetitions,
                    "tokens": 64000,
                    "wall_seconds": 600,
                },
            },
            "optimize": {
                "executor": "local-process",
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 1800},
            },
            "final_evaluate": {
                "executor": "local-process",
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 300},
            },
            "aggregate": {
                "executor": "local-process",
                "budget": {"model_calls": 0, "tokens": 0, "wall_seconds": 300},
            },
        },
        "evaluation": {
            "repository": {"url": repository_url, "ref": repository_ref},
            "instructions": {
                "global": list(global_instructions),
                "skills": dict(skills),
            },
            "case_authoring": {
                "instructions": "case-authoring.md",
            },
            "cases": {
                "directory": cases_directory,
                "coverage": {
                    "catalog": f"{cases_directory}/coverage.yaml",
                    "minimum_cases_per_skill": 8,
                    "minimum_leakage_groups_per_skill": 4,
                },
            },
            "splits": {"file": f"{cases_directory}/splits.yaml"},
            "judge": {
                "dspy": _program("judge"),
                "calibration": {"directory": "judge-calibration"},
                "repetitions": judge_repetitions,
            },
        },
        "experiments": {
            "baseline": {
                "mode": "evaluate",
                "solver": {
                    "dspy": _program("solver"),
                },
                "runtime": runtime,
                "repetitions": baseline_repetitions,
            }
        },
    }
    if include_optimization:
        experiments = payload["experiments"]
        assert isinstance(experiments, dict)
        experiments["optimize-few-shot"] = {
            "mode": "optimize",
            "based_on": "baseline",
            "dataset": {
                "train": "train",
                "development": "development",
                "final_evaluation": "test",
            },
            "optimizer": {"name": "LabeledFewShot", "parameters": {"k": 2, "seed": 0}},
            "budget": {
                "solver": {"model_calls": 100, "tokens": 250000},
                "judge": {"model_calls": 300, "tokens": 500000},
                "wall_seconds": 1800,
            },
            "output": {"compiled_program": "content_addressed_json"},
        }
    authoring = (
        files("ms_agent_eval")
        .joinpath("templates/case-authoring.md")
        .read_text(encoding="utf-8")
    )
    case_bank_readme = (
        files("ms_agent_eval")
        .joinpath("templates/cases-readme.md")
        .read_text(encoding="utf-8")
    )
    capability_catalog = (
        "# Mandatory semantic coverage contract. Read README.md before editing.\n"
        "# Keep status draft until every exact selected skill is fully decomposed.\n"
        "schema_version: 1\n"
        "status: draft\n"
        "default_minimum_primary_cases: 3\n"
        "skills: {}\n"
    )
    return WorkspaceTemplate(
        payload=payload,
        cases_directory=cases_directory,
        api_key_env=api_key_env,
        case_authoring_instructions=authoring,
        capability_catalog=capability_catalog,
        case_bank_readme=case_bank_readme,
    )


def planned_workspace_paths(
    workspace_file: Path,
    template: WorkspaceTemplate,
    *,
    include_api_key: bool = False,
) -> tuple[Path, ...]:
    root = workspace_file.resolve().parent
    paths = [
        workspace_file.resolve(),
        root / "case-authoring.md",
        root / template.cases_directory / "README.md",
        root / template.cases_directory / "coverage.yaml",
        root / template.cases_directory / "splits.yaml",
        root / "judge-calibration" / "manifest.yaml",
    ]
    if template.env_example is not None:
        paths.append(root / ".env.example")
        if not (root / ".gitignore").exists():
            paths.append(root / ".gitignore")
    if template.api_key_env is not None and include_api_key:
        paths.append(root / ".env")
    return tuple(paths)


def _atomic_text(path: Path, value: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_workspace_template(
    workspace_file: Path,
    template: WorkspaceTemplate,
    *,
    api_key: str | None = None,
) -> Mapping[str, str]:
    workspace_file = workspace_file.resolve()
    if workspace_file.exists():
        raise IntegrityError(f"workspace already exists: {workspace_file}")
    root = workspace_file.parent
    cases_root = root / template.cases_directory
    if cases_root.is_absolute() and root not in cases_root.parents:
        raise ConfigurationError("cases directory must remain inside the workspace")
    calibration = root / "judge-calibration"
    files: dict[Path, str] = {
        workspace_file: template.workspace_yaml,
        root / "case-authoring.md": template.case_authoring_instructions,
        cases_root / "README.md": template.case_bank_readme,
        cases_root / "coverage.yaml": template.capability_catalog,
        cases_root / "splits.yaml": "schema_version: 2\ngroups: {}\n",
        calibration / "manifest.yaml": "schema_version: 2\nfixtures: []\n",
    }
    if template.env_example is not None:
        files[root / ".env.example"] = template.env_example
        gitignore = root / ".gitignore"
        if not gitignore.exists():
            files[gitignore] = ".env\n"
    dotenv = root / ".env"
    if template.api_key_env is not None and api_key:
        if any(character in api_key for character in ("\n", "\r", "\x00")):
            raise ConfigurationError("API key must be a single-line value")
        files[dotenv] = f"{template.api_key_env}={api_key}\n"
    existing = [path for path in files if path.exists()]
    if existing:
        raise IntegrityError(f"setup refuses to overwrite existing file: {existing[0]}")
    directory_state = {
        root: root.exists(),
        cases_root: cases_root.exists(),
        calibration: calibration.exists(),
    }
    created: list[Path] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
        cases_root.mkdir(parents=True, exist_ok=True)
        calibration.mkdir(parents=True, exist_ok=True)
        for path, value in files.items():
            _atomic_text(path, value, mode=0o600 if path == dotenv else 0o644)
            created.append(path)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        for directory in (calibration, cases_root, root):
            if not directory_state[directory]:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        raise
    result = {
        "workspace": workspace_file.as_posix(),
        "case_authoring": (root / "case-authoring.md").as_posix(),
        "case_bank_readme": (cases_root / "README.md").as_posix(),
        "capability_catalog": (cases_root / "coverage.yaml").as_posix(),
        "cases": cases_root.as_posix(),
        "calibration": (root / "judge-calibration").as_posix(),
    }
    if template.env_example is not None:
        result["env_example"] = (root / ".env.example").as_posix()
        if root / ".gitignore" in created:
            result["gitignore"] = (root / ".gitignore").as_posix()
    return result
