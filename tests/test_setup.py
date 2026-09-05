from __future__ import annotations

import stat
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from ms_agent_eval.core.discovery import InspectedRepository, inspect_checkout
from ms_agent_eval.core.errors import ResolutionError
from ms_agent_eval.core.model_catalog import ModelCatalog
from ms_agent_eval.core.models import SourceRef, SourceRefKind
from ms_agent_eval.core.setup import SetupDependencies, run_guided_setup
from ms_agent_eval.core.sources import ResolvedSource
from ms_agent_eval.core.workspace import WorkspaceRepository
from ms_agent_eval.ui import Choice


class ScriptedPrompt:
    def __init__(
        self,
        *,
        texts: Sequence[str],
        selects: Sequence[str],
        checkboxes: Sequence[Sequence[str]],
        confirms: Sequence[bool],
        secrets: Sequence[str] = (),
    ) -> None:
        self.texts = iter(texts)
        self.selects = iter(selects)
        self.checkboxes = iter(checkboxes)
        self.confirms = iter(confirms)
        self.secrets = iter(secrets)
        self.output: list[str] = []

    def text(self, message: str, *, default: str | None = None) -> str:
        del message, default
        return next(self.texts)

    def secret(self, message: str) -> str:
        del message
        return next(self.secrets)

    def select(self, message: str, choices: Sequence[Choice]) -> str:
        del message
        value = next(self.selects)
        assert value in {item.value for item in choices}
        return value

    def checkbox(self, message: str, choices: Sequence[Choice]) -> tuple[str, ...]:
        del message
        values = tuple(next(self.checkboxes))
        assert set(values) <= {item.value for item in choices}
        return values

    def confirm(self, message: str, *, default: bool = False) -> bool:
        del message, default
        return next(self.confirms)

    def write(self, message: str) -> None:
        self.output.append(message)


def _checkout(root: Path) -> Path:
    checkout = root / "checkout"
    (checkout / "skills" / "alpha").mkdir(parents=True)
    (checkout / "skills" / "nested" / "beta").mkdir(parents=True)
    (checkout / "AGENTS.md").write_text("global", encoding="utf-8")
    (checkout / "skills" / "alpha" / "SKILL.md").write_text("alpha", encoding="utf-8")
    (checkout / "skills" / "nested" / "beta" / "SKILL.md").write_text(
        "beta", encoding="utf-8"
    )
    return checkout


def _dependencies(
    checkout: Path, *, expected_api_key: str | None = None
) -> SetupDependencies:
    resolved = ResolvedSource(
        "https://github.com/example/project",
        "https://github.com/example/project.git",
        SourceRef(SourceRefKind.TAG, "v1.0.0"),
        "a" * 40,
    )

    @contextmanager
    def inspector(repository_url: str, repository_ref: str, *, temporary_root: Path):  # type: ignore[no-untyped-def]
        del temporary_root
        assert repository_url == resolved.repository_url_requested
        assert repository_ref == "v1.0.0"
        yield InspectedRepository(resolved, checkout, inspect_checkout(checkout))

    def models(base_url: str, *, api_key: str | None = None) -> ModelCatalog:
        assert base_url == "http://127.0.0.1:8000/v1"
        assert api_key == expected_api_key
        return ModelCatalog(base_url, ("builder", "judge", "solver"))

    return SetupDependencies(tags=lambda _: ("v1.0.0",), inspect=inspector, models=models)


def test_guided_setup_selects_complete_skill_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "guided" / "workspace.yaml"
    data_root = tmp_path / "external"
    prompt = ScriptedPrompt(
        texts=(
            "guided-evaluation",
            data_root.as_posix(),
            "https://github.com/example/project",
            "http://127.0.0.1:8000/v1",
            "1",
            "3",
            "3.12",
            "cases",
            "Cover every selected skill",
        ),
        selects=(
            "v1.0.0",
            "directory",
            "skills",
            "builder",
            "solver",
            "judge",
            "response_only",
        ),
        checkboxes=(("AGENTS.md",),),
        confirms=(False, True, True, True),
    )

    result = run_guided_setup(
        prompt,
        workspace_file=workspace,
        dependencies=_dependencies(_checkout(tmp_path)),
    )

    assert result["status"] == "configured"
    repository = WorkspaceRepository.from_file(workspace)
    assert repository.workspace.schema_version == 4
    assert repository.case_authoring_path == workspace.parent / "case-authoring.md"
    assert "Evaluation Case Authoring Contract" in repository.case_authoring_instructions()
    assert repository.workspace.evaluation.instructions.skills.directory == "skills"
    assert repository.workspace.evaluation.case_builder.model.model == "builder"
    assert repository.workspace.evaluation.case_builder.model.base_url.endswith("/v1")
    assert not (workspace.parent / ".env").exists()
    assert not (workspace.parent / ".env.example").exists()
    assert result["resolved_commit"] == "a" * 40
    assert result["skill_count"] == 2


def test_guided_setup_records_only_explicit_skill_files(tmp_path: Path) -> None:
    workspace = tmp_path / "exact" / "workspace.yaml"
    data_root = tmp_path / "external-exact"
    prompt = ScriptedPrompt(
        texts=(
            "exact-evaluation",
            data_root.as_posix(),
            "https://github.com/example/project",
            "http://127.0.0.1:8000/v1",
            "builder-manual",
            "solver-manual",
            "judge-manual",
            "1",
            "3",
            "3.12",
            "cases",
            "Cover the selected skill",
        ),
        selects=("v1.0.0", "files", "response_only"),
        checkboxes=(("AGENTS.md",), ("skills/alpha/SKILL.md",)),
        confirms=(False, False, False, True),
    )

    result = run_guided_setup(
        prompt,
        workspace_file=workspace,
        dependencies=_dependencies(_checkout(tmp_path)),
    )

    assert result["status"] == "configured"
    payload = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    assert payload["evaluation"]["instructions"]["skills"] == {
        "files": ["skills/alpha/SKILL.md"]
    }
    assert result["skill_count"] == 1


def test_guided_setup_cancellation_writes_no_workspace_files(tmp_path: Path) -> None:
    workspace = tmp_path / "cancelled" / "workspace.yaml"
    data_root = tmp_path / "external-cancelled"
    prompt = ScriptedPrompt(
        texts=(
            "cancelled-evaluation",
            data_root.as_posix(),
            "https://github.com/example/project",
            "http://127.0.0.1:8000/v1",
            "1",
            "3",
            "3.12",
            "cases",
            "Cover every selected skill",
        ),
        selects=(
            "v1.0.0",
            "directory",
            "skills",
            "builder",
            "solver",
            "judge",
            "response_only",
        ),
        checkboxes=(("AGENTS.md",),),
        confirms=(False, True, True, False),
    )

    result = run_guided_setup(
        prompt,
        workspace_file=workspace,
        dependencies=_dependencies(_checkout(tmp_path)),
    )

    assert result["status"] == "cancelled"
    assert not workspace.exists()
    assert not (workspace.parent / "case-authoring.md").exists()
    assert not (workspace.parent / "cases" / "splits.yaml").exists()
    assert not (workspace.parent / "judge-calibration" / "manifest.yaml").exists()


def test_authenticated_setup_keeps_only_the_secret_outside_yaml(tmp_path: Path) -> None:
    workspace = tmp_path / "authenticated" / "workspace.yaml"
    data_root = tmp_path / "external-authenticated"
    api_key = "private-api-key"
    prompt = ScriptedPrompt(
        texts=(
            "authenticated-evaluation",
            data_root.as_posix(),
            "https://github.com/example/project",
            "http://127.0.0.1:8000/v1",
            "1",
            "3",
            "3.12",
            "cases",
            "Cover every selected skill",
        ),
        selects=(
            "v1.0.0",
            "directory",
            "skills",
            "builder",
            "solver",
            "judge",
            "response_only",
        ),
        checkboxes=(("AGENTS.md",),),
        confirms=(True, True, True, True),
        secrets=(api_key,),
    )

    run_guided_setup(
        prompt,
        workspace_file=workspace,
        dependencies=_dependencies(_checkout(tmp_path), expected_api_key=api_key),
    )

    workspace_text = workspace.read_text(encoding="utf-8")
    dotenv = workspace.parent / ".env"
    example = workspace.parent / ".env.example"
    assert "api_key_env: OPENAI_API_KEY" in workspace_text
    assert api_key not in workspace_text
    assert dotenv.read_text(encoding="utf-8") == f"OPENAI_API_KEY={api_key}\n"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    assert api_key not in example.read_text(encoding="utf-8")
    assert (workspace.parent / ".gitignore").read_text(encoding="utf-8") == ".env\n"
    assert api_key not in "\n".join(prompt.output)


def test_skill_id_collision_fails_before_workspace_write(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    (checkout / "skills" / "SKILL.md").write_text("root skill", encoding="utf-8")
    (checkout / "skills" / "skills").mkdir()
    (checkout / "skills" / "skills" / "SKILL.md").write_text(
        "colliding skill", encoding="utf-8"
    )
    workspace = tmp_path / "collision" / "workspace.yaml"
    prompt = ScriptedPrompt(
        texts=(
            "collision-evaluation",
            (tmp_path / "external-collision").as_posix(),
            "https://github.com/example/project",
            "http://127.0.0.1:8000/v1",
            "1",
            "3",
            "3.12",
            "cases",
            "Cover every selected skill",
        ),
        selects=(
            "v1.0.0",
            "directory",
            "skills",
            "builder",
            "solver",
            "judge",
            "response_only",
        ),
        checkboxes=(("AGENTS.md",),),
        confirms=(False, True, True),
    )

    with pytest.raises(ResolutionError, match="instruction unit collision"):
        run_guided_setup(
            prompt,
            workspace_file=workspace,
            dependencies=_dependencies(checkout),
        )
    assert not workspace.exists()
