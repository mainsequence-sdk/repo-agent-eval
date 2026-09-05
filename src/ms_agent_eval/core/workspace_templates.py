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
    "case_builder": {
        "inputs": {
            "global_context": "str",
            "skill_context": "str",
            "source_context": "str",
            "coverage_request": "str",
            "existing_case_summaries": "list[str]",
        },
        "outputs": {
            "case_spec": "dict[str, object]",
            "prompt": "str",
            "expected_response": "str",
            "rubric": "dict[str, object]",
            "expected_artifacts": "dict[str, str]",
            "source_paths": "list[str]",
            "leakage_group": "str",
        },
    },
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
    base_url: str,
    api_key_env: str | None,
    temperature: float,
) -> Mapping[str, object]:
    result: dict[str, object] = {
        "provider": "openai_compatible",
        "name": name,
        "base_url": base_url,
        "parameters": {"temperature": temperature},
    }
    if api_key_env is not None:
        result["api_key_env"] = api_key_env
    return result


def build_workspace_template(
    *,
    workspace_id: str,
    repository_url: str,
    repository_ref: str,
    global_instructions: Sequence[str],
    skills: Mapping[str, object],
    cases_directory: str,
    api_base_url: str,
    case_builder_model: str,
    solver_model: str,
    judge_model: str,
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
        "schema_version": 4,
        "workspace": workspace,
        "evaluation": {
            "repository": {"url": repository_url, "ref": repository_ref},
            "instructions": {
                "global": list(global_instructions),
                "skills": dict(skills),
            },
            "case_builder": {
                "instructions": "case-authoring.md",
                "dspy": _program("case_builder"),
                "model": _model(
                    name=case_builder_model,
                    base_url=api_base_url,
                    api_key_env=api_key_env,
                    temperature=0.2,
                ),
                "budget": {"model_calls": 100, "tokens": 500000},
                "output": {"drafts": "external", "promotion": "explicit"},
            },
            "cases": {"directory": cases_directory},
            "splits": {"file": f"{cases_directory}/splits.yaml"},
            "judge": {
                "dspy": _program("judge"),
                "model": _model(
                    name=judge_model,
                    base_url=api_base_url,
                    api_key_env=api_key_env,
                    temperature=0.0,
                ),
                "calibration": {"directory": "judge-calibration"},
                "repetitions": judge_repetitions,
            },
        },
        "experiments": {
            "baseline": {
                "mode": "evaluate",
                "solver": {
                    "dspy": _program("solver"),
                    "model": _model(
                        name=solver_model,
                        base_url=api_base_url,
                        api_key_env=api_key_env,
                        temperature=0.2,
                    ),
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
    return WorkspaceTemplate(payload, cases_directory, api_key_env, authoring)


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
        "cases": cases_root.as_posix(),
        "calibration": (root / "judge-calibration").as_posix(),
    }
    if template.env_example is not None:
        result["env_example"] = (root / ".env.example").as_posix()
        if root / ".gitignore" in created:
            result["gitignore"] = (root / ".gitignore").as_posix()
    return result
