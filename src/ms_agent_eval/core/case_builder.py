from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import dspy
import yaml

from ms_agent_eval.programs.dspy.engine import (
    DspyExecutionContract,
    DspyExecutor,
    create_case_builder_program,
    program_hash,
)

from .errors import ConfigurationError, IntegrityError, PreflightError, ResolutionError
from .evaluation import CaseDefinition
from .hashing import canonical_json_bytes, content_hash, json_value
from .models import ArtifactReference, ProgramResult, SnapshotLock
from .providers import ModelCallObserver
from .storage import FilesystemArtifactStore
from .workspace import ResolvedRoleModel, WorkspaceRepository


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class SnapshotContext:
    snapshot_hash: str
    resolved_commit: str
    global_context: str
    skill_contexts: Mapping[str, str]
    source_paths: tuple[str, ...]
    source_files: Mapping[str, Path]

    def skill(self, skill_id: str) -> str:
        try:
            return self.skill_contexts[skill_id]
        except KeyError as error:
            raise ResolutionError(f"snapshot does not contain skill {skill_id!r}") from error

    def supporting_source(
        self,
        skill_id: str,
        coverage_request: str,
        *,
        maximum_characters: int = 60000,
    ) -> str:
        """Select bounded text source context without deciding evaluation semantics."""

        terms = {
            item.lower()
            for item in (skill_id.replace("/", " ") + " " + coverage_request).split()
            if len(item) >= 3
        }
        candidates: list[tuple[int, str, str]] = []
        for source_path, path in self.source_files.items():
            if source_path.endswith((".png", ".jpg", ".jpeg", ".gif", ".zip", ".pdf")):
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    value = handle.read(20000)
            except (OSError, UnicodeDecodeError):
                continue
            normalized = (source_path + " " + value).lower()
            score = sum(term in normalized for term in terms)
            if score or source_path in self.source_paths:
                candidates.append((score, source_path, value))
                if len(candidates) > 400:
                    candidates = sorted(candidates, key=lambda item: (-item[0], item[1]))[:200]
        parts: list[str] = []
        size = 0
        for _, source_path, value in sorted(candidates, key=lambda item: (-item[0], item[1])):
            section = f"# {source_path}\n\n{value}\n"
            if size + len(section) > maximum_characters:
                remaining = maximum_characters - size
                if remaining > len(source_path) + 10:
                    parts.append(section[:remaining])
                break
            parts.append(section)
            size += len(section)
        return "\n".join(parts)


def load_snapshot_context(
    lock: SnapshotLock,
    snapshot_directory: Path,
    *,
    global_paths: Sequence[str] | None = None,
) -> SnapshotContext:
    files = {item.source_path: item for item in lock.files}
    unit_paths = {item.source_path for item in lock.units}
    selected_globals = set(global_paths) if global_paths is not None else set(files) - unit_paths
    global_parts: list[str] = []
    for source_path, item in files.items():
        if source_path not in selected_globals:
            continue
        global_parts.append(
            f"# {source_path}\n\n"
            + (snapshot_directory / item.snapshot_path).read_text(encoding="utf-8")
        )
    skills = {
        item.unit_id: (snapshot_directory / item.snapshot_path).read_text(encoding="utf-8")
        for item in lock.units
    }
    return SnapshotContext(
        snapshot_hash=lock.content_hash,
        resolved_commit=lock.resolved_commit,
        global_context="\n\n".join(global_parts),
        skill_contexts=skills,
        source_paths=tuple(sorted(files)),
        source_files={
            source_path: snapshot_directory / item.snapshot_path
            for source_path, item in files.items()
        },
    )


@dataclass(frozen=True)
class CaseDraft:
    id: str
    case_id: str
    skill: str
    directory: Path
    manifest: Path
    package_hash: str
    builder_model_hash: str
    builder_program_hash: str
    source_snapshot_hash: str
    generation_request_hash: str
    request_artifact: ArtifactReference
    result_artifact: ArtifactReference
    call_ids: tuple[str, ...]
    status: str

    @classmethod
    def load(cls, directory: Path) -> CaseDraft:
        manifest = directory / "draft.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ResolutionError(f"invalid case draft: {directory}") from error
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 2:
            raise ConfigurationError(f"invalid draft manifest: {manifest}")
        request = _reference(payload.get("request_artifact"), manifest)
        result = _reference(payload.get("result_artifact"), manifest)
        call_ids = payload.get("call_ids")
        if not isinstance(call_ids, Sequence) or isinstance(call_ids, (str, bytes)):
            raise ConfigurationError(f"draft call_ids must be a list: {manifest}")
        return cls(
            id=_text(payload.get("id"), "draft.id"),
            case_id=_text(payload.get("case_id"), "draft.case_id"),
            skill=_text(payload.get("skill"), "draft.skill"),
            directory=directory,
            manifest=manifest,
            package_hash=_text(payload.get("package_hash"), "draft.package_hash"),
            builder_model_hash=_text(payload.get("builder_model_hash"), "draft.builder_model_hash"),
            builder_program_hash=_text(
                payload.get("builder_program_hash"), "draft.builder_program_hash"
            ),
            source_snapshot_hash=_text(
                payload.get("source_snapshot_hash"), "draft.source_snapshot_hash"
            ),
            generation_request_hash=_text(
                payload.get("generation_request_hash"), "draft.generation_request_hash"
            ),
            request_artifact=request,
            result_artifact=result,
            call_ids=tuple(map(str, call_ids)),
            status=_text(payload.get("status"), "draft.status"),
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value


def _reference(value: object, path: Path) -> ArtifactReference:
    if not isinstance(value, Mapping):
        raise ConfigurationError("draft artifact reference must be a mapping", path=path)
    size = value.get("size_bytes")
    if not isinstance(size, int) or size < 0:
        raise ConfigurationError("draft artifact size is invalid", path=path)
    return ArtifactReference(
        _text(value.get("content_id"), "artifact.content_id"),
        _text(value.get("media_type"), "artifact.media_type"),
        size,
        _text(value.get("relative_path"), "artifact.relative_path"),
    )


class CaseDraftStore:
    def __init__(
        self,
        data_root: Path,
        *,
        workspace_root: Path,
        artifacts: FilesystemArtifactStore,
    ) -> None:
        self.data_root = data_root
        self.workspace_root = workspace_root.resolve()
        self.artifacts = artifacts
        self.root = data_root / "case-drafts"
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        *,
        output: Mapping[str, object],
        request: Mapping[str, object],
        result: ProgramResult,
        skill: str,
        source_snapshot_hash: str,
        builder_model_hash: str,
        builder_program_hash: str,
        allowed_source_paths: Sequence[str],
        existing_case_ids: Sequence[str],
    ) -> CaseDraft:
        parsed = self._validate_output(
            output,
            skill=skill,
            allowed_source_paths=allowed_source_paths,
            existing_case_ids=existing_case_ids,
        )
        request_hash = content_hash(request)
        request_reference = self.artifacts.put_manifest(
            f"case-builder/requests/{request_hash.removeprefix('sha256:')}", request
        )
        result_document = {
            "status": result.status,
            "outputs": result.outputs,
            "calls": result.calls,
            "trace_artifact": result.trace_artifact,
            "error_kind": result.error_kind,
        }
        result_reference = self.artifacts.put_manifest(
            f"case-builder/results/{content_hash(result_document).removeprefix('sha256:')}",
            json_value(result_document),  # type: ignore[arg-type]
        )
        staging = Path(tempfile.mkdtemp(prefix="case-draft-", dir=self.data_root / "tmp"))
        try:
            case_directory = staging / "package"
            case_directory.mkdir()
            expected_directory = case_directory / "expected"
            expected_directory.mkdir()
            (case_directory / "prompt.md").write_text(parsed["prompt"], encoding="utf-8")
            (expected_directory / "response.md").write_text(
                parsed["expected_response"], encoding="utf-8"
            )
            expected_artifacts = parsed["expected_artifacts"]
            if expected_artifacts:
                artifacts_directory = expected_directory / "artifacts"
                for relative, value in expected_artifacts.items():
                    path = artifacts_directory / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="utf-8")
            (case_directory / "rubric.yaml").write_text(
                yaml.safe_dump(parsed["rubric"], sort_keys=False), encoding="utf-8"
            )
            case_payload: dict[str, object] = {
                "schema_version": 2,
                "id": parsed["case_id"],
                "title": parsed["title"],
                "skill": skill,
                "group": parsed["group"],
                "prompt": "prompt.md",
                "expected": "expected/response.md",
                "rubric": "rubric.yaml",
                "source_paths": parsed["source_paths"],
                "provenance": {
                    "builder_model_hash": builder_model_hash,
                    "builder_program_hash": builder_program_hash,
                    "source_snapshot_hash": source_snapshot_hash,
                    "generation_request_hash": request_hash,
                    "draft_content_hash": "pending",
                },
            }
            if expected_artifacts:
                case_payload["expected_artifacts"] = "expected/artifacts"
            case_file = case_directory / "case.yaml"
            case_file.write_text(yaml.safe_dump(case_payload, sort_keys=False), encoding="utf-8")
            package_hash = CaseDefinition.load(case_directory, verify_provenance=False).content_hash
            provenance = case_payload["provenance"]
            assert isinstance(provenance, dict)
            provenance["draft_content_hash"] = package_hash
            case_file.write_text(yaml.safe_dump(case_payload, sort_keys=False), encoding="utf-8")
            CaseDefinition.load(case_directory)
            draft_identity = {
                "package_hash": package_hash,
                "source_snapshot_hash": source_snapshot_hash,
                "builder_model_hash": builder_model_hash,
                "builder_program_hash": builder_program_hash,
                "generation_request_hash": request_hash,
            }
            draft_id = content_hash(draft_identity).removeprefix("sha256:")
            destination = self.root / draft_id
            manifest_payload = {
                "schema_version": 2,
                "id": draft_id,
                "case_id": parsed["case_id"],
                "skill": skill,
                **draft_identity,
                "request_artifact": request_reference,
                "result_artifact": result_reference,
                "call_ids": [item.call_id for item in result.calls],
                "status": "validated",
            }
            (staging / "draft.json").write_bytes(canonical_json_bytes(manifest_payload))
            if destination.exists():
                existing = CaseDraft.load(destination)
                if existing.package_hash != package_hash:
                    raise IntegrityError("content-addressed case draft identity collision")
                return existing
            os.replace(staging, destination)
            return CaseDraft.load(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def list(self) -> tuple[CaseDraft, ...]:
        return tuple(
            CaseDraft.load(path)
            for path in sorted(self.root.iterdir())
            if path.is_dir() and not path.is_symlink()
        )

    def promote(self, draft_id: str, cases_root: Path) -> CaseDefinition:
        draft = CaseDraft.load(self.root / draft_id)
        if draft.status != "validated":
            raise PreflightError(f"draft {draft_id!r} is not validated")
        source = draft.directory / "package"
        case = CaseDefinition.load(source)
        destination = cases_root / case.skill / case.id
        resolved_root = cases_root.resolve()
        resolved_destination = destination.resolve()
        if resolved_root not in resolved_destination.parents:
            raise IntegrityError("promoted case path escapes the cases directory")
        if destination.exists():
            raise IntegrityError(f"case already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        promoted = CaseDefinition.load(destination)
        manifest_payload = json.loads(draft.manifest.read_text(encoding="utf-8"))
        manifest_payload["status"] = "promoted"
        manifest_payload["promoted_to"] = destination.relative_to(cases_root).as_posix()
        draft.manifest.write_bytes(canonical_json_bytes(manifest_payload))
        return promoted

    @staticmethod
    def _validate_output(
        output: Mapping[str, object],
        *,
        skill: str,
        allowed_source_paths: Sequence[str],
        existing_case_ids: Sequence[str],
    ) -> Mapping[str, object]:
        case_spec = output.get("case_spec")
        if not isinstance(case_spec, Mapping):
            raise IntegrityError("case-builder case_spec must be a mapping")
        unknown_case_spec = sorted(set(map(str, case_spec)) - {"id", "title", "skill"})
        if unknown_case_spec:
            raise IntegrityError(
                f"case-builder case_spec contains unknown fields: {unknown_case_spec}"
            )
        case_id = _text(case_spec.get("id"), "case_spec.id")
        if _IDENTIFIER.fullmatch(case_id) is None:
            raise IntegrityError("case-builder case id must be a lowercase identifier")
        if case_id in existing_case_ids:
            raise IntegrityError(f"case-builder returned existing case id {case_id!r}")
        title = _text(case_spec.get("title"), "case_spec.title")
        declared_skill = case_spec.get("skill", skill)
        if declared_skill != skill:
            raise IntegrityError("case-builder changed the selected skill id")
        group = _text(output.get("leakage_group"), "leakage_group")
        if _IDENTIFIER.fullmatch(group) is None:
            raise IntegrityError("case-builder leakage group must be a lowercase identifier")
        prompt = _text(output.get("prompt"), "prompt")
        expected_response = _text(output.get("expected_response"), "expected_response")
        if prompt.strip() == expected_response.strip():
            raise IntegrityError("case prompt and expected response must differ")
        rubric = output.get("rubric")
        if not isinstance(rubric, Mapping):
            raise IntegrityError("case-builder rubric must be a mapping")
        artifacts = output.get("expected_artifacts")
        if not isinstance(artifacts, Mapping):
            raise IntegrityError("case-builder expected_artifacts must be a mapping")
        safe_artifacts: dict[str, str] = {}
        for raw_path, raw_value in artifacts.items():
            if not isinstance(raw_path, str):
                raise IntegrityError("case-builder expected artifact paths must be strings")
            relative = PurePosixPath(raw_path)
            if (
                relative.is_absolute()
                or relative.as_posix() in {"", "."}
                or ".." in relative.parts
                or "\x00" in raw_path
                or ":" in relative.parts[0]
                or not isinstance(raw_value, str)
            ):
                raise IntegrityError("case-builder returned an unsafe expected artifact")
            normalized = relative.as_posix()
            if normalized in safe_artifacts:
                raise IntegrityError("case-builder returned duplicate expected artifact paths")
            safe_artifacts[normalized] = raw_value
        raw_sources = output.get("source_paths")
        if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
            raise IntegrityError("case-builder source_paths must be a list")
        source_paths = tuple(map(str, raw_sources))
        if not source_paths:
            raise IntegrityError("case-builder must ground the case in source paths")
        unknown = sorted(set(source_paths) - set(allowed_source_paths))
        if unknown:
            raise IntegrityError(f"case-builder referenced paths absent from snapshot: {unknown}")
        return {
            "case_id": case_id,
            "title": title,
            "group": group,
            "prompt": prompt,
            "expected_response": expected_response,
            "rubric": dict(rubric),
            "expected_artifacts": safe_artifacts,
            "source_paths": source_paths,
        }


class CaseBuilderService:
    def __init__(
        self,
        repository: WorkspaceRepository,
        context: SnapshotContext,
        artifacts: FilesystemArtifactStore,
        drafts: CaseDraftStore,
    ) -> None:
        self.repository = repository
        self.context = context
        self.artifacts = artifacts
        self.drafts = drafts

    def build(
        self,
        *,
        skill: str,
        coverage_request: str,
        model: ResolvedRoleModel,
        lm_factory: Callable[[ModelCallObserver], dspy.BaseLM],
    ) -> CaseDraft:
        if model.role != "case_builder":
            raise IntegrityError("case builder requires the case_builder model role")
        program = create_case_builder_program(
            self.repository.case_authoring_instructions()
        )
        existing = []
        existing_ids = []
        if self.repository.cases_root.exists():
            for path in self.repository.case_paths():
                case = CaseDefinition.load(path.parent)
                existing.append(f"{case.id}: {case.title} [{case.group}]")
                existing_ids.append(case.id)
        values = {
            "global_context": self.context.global_context,
            "skill_context": self.context.skill(skill),
            "source_context": self.context.supporting_source(skill, coverage_request),
            "coverage_request": coverage_request,
            "existing_case_summaries": existing,
        }
        request = {
            "role": "case_builder",
            "model_hash": model.content_hash,
            "program_hash": program_hash(program),
            "source_snapshot_hash": self.context.snapshot_hash,
            "skill": skill,
            "inputs": values,
        }
        observer = ModelCallObserver(self.artifacts, role="case_builder")
        lm = lm_factory(observer)
        result = DspyExecutor(self.artifacts).execute(
            contract=DspyExecutionContract(
                role="case_builder",
                program_hash=program_hash(program),
                inputs=values,
                required_outputs=(
                    "case_spec",
                    "prompt",
                    "expected_response",
                    "rubric",
                    "expected_artifacts",
                    "source_paths",
                    "leakage_group",
                ),
                primary_output=None,
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed":
            raise IntegrityError(f"case-builder DSPy call failed: {result.error_kind}")
        return self.drafts.publish(
            output=result.outputs,
            request=request,
            result=result,
            skill=skill,
            source_snapshot_hash=self.context.snapshot_hash,
            builder_model_hash=model.content_hash,
            builder_program_hash=program_hash(program),
            allowed_source_paths=self.context.source_paths,
            existing_case_ids=existing_ids,
        )
