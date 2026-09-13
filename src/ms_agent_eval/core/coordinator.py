from __future__ import annotations

import json
import random
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from uuid import uuid4

import dspy

from ms_agent_eval.programs.dspy.budget import BudgetLedger, BudgetLimits
from ms_agent_eval.programs.dspy.engine import (
    DspyExecutionContract,
    DspyExecutor,
    create_author_program,
    create_judge_program,
    create_solver_program,
    load_state_json,
    program_hash,
)
from ms_agent_eval.providers.openai_compatible import create_observed_lm

from .errors import (
    AgentEvalError,
    ConfigurationError,
    IntegrityError,
    PreflightError,
    ResolutionError,
)
from .authoring import SnapshotContext, SnapshotSourceIdentity
from .evaluation import (
    CalibrationCorpus,
    CapabilityCatalog,
    CaseBank,
    LlmJudge,
    SplitManifest,
    calibrate_judge,
    calibration_result_from_scores,
)
from .garbage_collection import collect_artifact_garbage
from .hashing import canonical_json_bytes, content_hash, json_value
from .models import ArtifactReference, SnapshotLock
from .planning import CompiledWorkspace, ExperimentLock, lock_as_dict
from .progress import ProgressEvent, ProgressSink
from .providers import ModelCallObserver
from .reporting import PublishedReport, build_and_publish_run_report
from .stage_executors import (
    AutomaticExecutionFailed,
    ExecutorOutput,
    HarnessExecutionPending,
)
from .stages import (
    StageArtifact,
    StageBudget,
    StageError,
    StageExecutorIdentity,
    StageKind,
    StageLedger,
    StageModelIdentity,
    StagePacket,
    StageResult,
    StageState,
)
from .storage import FilesystemArtifactStore
from .snapshots import ExternalSnapshotStore
from .workspace import (
    ExecutorConfiguration,
    ExperimentMode,
    ResolvedRoleModel,
    RoleBudget,
    WorkspaceRepository,
    load_yaml,
    resolve_stage_model,
)


LmFactory = Callable[
    [ResolvedRoleModel, ModelCallObserver, BudgetLedger | None, str | None], dspy.BaseLM
]


def default_lm_factory(
    model: ResolvedRoleModel,
    observer: ModelCallObserver,
    budget: BudgetLedger | None,
    api_key: str | None,
) -> dspy.BaseLM:
    return create_observed_lm(model, observer, budget=budget, api_key=api_key)


@dataclass(frozen=True)
class RunProgress:
    schema_version: int
    run_id: str
    experiment_id: str
    execution_mode: str
    benchmark_valid: bool
    coverage_status: str
    status: str
    current_stage: str | None
    packets: tuple[Mapping[str, object], ...]
    result: Mapping[str, object] | None


class _ExecutionSession:
    def __init__(
        self,
        coordinator: StageCoordinator,
        lock: ExperimentLock,
        environment: Mapping[str, str],
    ) -> None:
        self.coordinator = coordinator
        self.lock = lock
        self.environment = environment
        self.observers: dict[str, list[ModelCallObserver]] = {}

    def lm(
        self, role: str, packet: StagePacket
    ) -> tuple[dspy.BaseLM, ModelCallObserver]:
        model = self.lock.models.get(role) or resolve_stage_model(
            self.coordinator.workspace.repository.workspace,
            packet.stage.value,
            role=role,
        )
        if model is None:
            raise ConfigurationError(f"stage {packet.stage.value!r} has no model")
        executor = self.coordinator.workspace.repository.workspace.executors[model.executor_id]
        observer = ModelCallObserver(
            self.coordinator.artifacts,
            role=role,
            run_id=packet.run_id,
            packet_id=packet.packet_id,
            work_id=packet.work_id,
            attempt_id=packet.attempt_id,
            experiment_revision_id=packet.experiment_revision_id,
            stage=packet.stage.value,
        )
        self.observers.setdefault(packet.packet_id, []).append(observer)
        lm = self.coordinator.lm_factory(
            model,
            observer,
            self.coordinator._budget_ledger(packet.stage),
            executor.api_key(self.environment),
        )
        return lm, observer

    def evidence(self, packet_id: str) -> Mapping[str, object]:
        observers = self.observers.get(packet_id, [])
        records = [record for observer in observers for record in observer.records]
        traces = [artifact for observer in observers for artifact in observer.trace_artifacts]
        if not records and not traces:
            return {}
        return {
            "model_call_ids": [record.call_id for record in records],
            "trace_artifacts": [json_value(artifact) for artifact in traces],
        }

    def usage(self, packet_id: str) -> Mapping[str, object]:
        return _usage(
            [
                record
                for observer in self.observers.get(packet_id, [])
                for record in observer.records
            ]
        )


class StageCoordinator:
    """Advance experiments exclusively through the portable stage contract."""

    def __init__(
        self,
        workspace: CompiledWorkspace | WorkspaceRepository,
        *,
        lm_factory: LmFactory = default_lm_factory,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self.workspace = workspace
        repository = (
            workspace
            if isinstance(workspace, WorkspaceRepository)
            else workspace.repository
        )
        self.artifacts = FilesystemArtifactStore(
            repository.data_root,
            workspace_root=repository.root,
        )
        self.ledger = StageLedger(self.artifacts)
        self.dspy = DspyExecutor(self.artifacts)
        self.lm_factory = lm_factory
        self.progress_sink = progress_sink

    def build_report(self, run_id: str, *, replace: bool = False) -> PublishedReport:
        """Build a deterministic derived report without executing a model."""

        run = self.ledger.load_run(run_id)
        repository = (
            self.workspace
            if isinstance(self.workspace, WorkspaceRepository)
            else self.workspace.repository
        )
        environment = repository.environment()
        if isinstance(self.workspace, WorkspaceRepository):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.build_report(run_id, replace=replace)
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.build_report(run_id, replace=replace)
        return build_and_publish_run_report(
            self.workspace,
            self.ledger,
            run_id,
            replace=replace,
        )

    def start(
        self,
        experiment_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
        pilot: bool = False,
    ) -> RunProgress:
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        if pilot and lock.configuration.mode is not ExperimentMode.EVALUATE:
            raise PreflightError("pilot execution supports evaluate experiments only")
        self._validate_calibration_split(lock, pilot=pilot)
        run_id = str(uuid4())
        execution_mode = "pilot" if pilot else "benchmark"
        revision_digest = lock.content_hash.removeprefix("sha256:")
        definition_artifact = self.ledger.put_json(
            "experiment_definition", self._experiment_definition(lock)
        )
        lock_reference = self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/experiment.lock",
            lock_as_dict(lock),
        )
        bundle_reference = self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/bundle",
            {
                "schema_version": 2,
                "experiment_family": experiment_id,
                "experiment_revision_id": lock.content_hash,
                "lock": json_value(lock_reference),
                "definition": json_value(definition_artifact),
                "source_snapshot_hash": lock.source_snapshot_hash,
                "case_bank_hash": lock.case_bank_hash,
                "split_hash": lock.split_hash,
                "calibration_hash": lock.calibration_hash,
                "case_ids": [item.id for item in lock.cases],
            },
        )
        self.ledger.create_run(
            run_id,
            {
                "schema_version": 4,
                "run_id": run_id,
                "experiment_id": experiment_id,
                "execution_mode": execution_mode,
                "benchmark_valid": not pilot,
                "coverage_status": self.workspace.validation["status"],
                "selected_splits": (
                    ["train", "validation"] if pilot else ["configured"]
                ),
                "judge_repetitions": (
                    1
                    if pilot
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
                "workspace_id": lock.workspace_id,
                "workspace_hash": lock.workspace_hash,
                "experiment_revision_id": lock.content_hash,
                "experiment_lock_hash": lock.content_hash,
                "experiment_lock_artifact": json_value(lock_reference),
                "experiment_bundle_artifact": json_value(bundle_reference),
                "experiment_definition_artifact": json_value(definition_artifact),
                "created_at": _timestamp(),
            },
        )
        self.artifacts.put_manifest(
            f"experiments/{experiment_id}/{revision_digest}/runs/{run_id}",
            {
                "schema_version": 1,
                "run_id": run_id,
                "experiment_family": experiment_id,
                "experiment_revision_id": lock.content_hash,
            },
        )
        self._emit(
            "run_started",
            run_id,
            experiment_id,
            details={
                "execution_mode": execution_mode,
                "case_count": self._run_case_count(
                    lock.configuration.mode,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
            },
        )
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def _experiment_definition(self, lock: ExperimentLock) -> Mapping[str, object]:
        """Freeze all static, stage-visible inputs needed by this revision."""

        file_inventory = self._workspace_file_inventory()
        return {
            "schema_version": 2,
            "experiment_family": lock.configuration.id,
            "experiment_revision_id": lock.content_hash,
            "lock": lock_as_dict(lock),
            "workspace_configuration_json": self._selected_workspace_configuration(lock),
            "workspace_files": file_inventory,
            "target_snapshot_hash": self.workspace.snapshot.content_hash,
            "validation": json_value(self.workspace.validation),
            "context": {
                "snapshot_hash": self.workspace.context.snapshot_hash,
                "resolved_commit": self.workspace.context.resolved_commit,
                "global": self.workspace.context.global_context,
                "skills": dict(self.workspace.context.skill_contexts),
                "sources": json_value(self.workspace.context.sources),
                "source_paths": list(self.workspace.context.source_paths),
            },
            "authoring": {
                "instructions": self.workspace.repository.case_authoring_instructions(),
                "capability_catalog": json_value(self.workspace.capability_catalog),
            },
            "cases": [
                {
                    "id": case.id,
                    "title": case.title,
                    "skill": case.skill,
                    "group": case.group,
                    "split": self.workspace.splits.split_for(case),
                    "prompt": case.prompt,
                    "solver_attachments": dict(case.solver_attachments()),
                    "expected_response": case.expected_response,
                    "expected_artifacts": dict(case.expected_artifacts()),
                    "rubric": json.loads(case.rubric.prompt_text()),
                    "passing_score": case.rubric.passing_score,
                    "source_paths": list(case.source_paths),
                    "solver_view_hash": case.solver_view_hash,
                    "judge_view_hash": case.judge_view_hash,
                    "metadata_hash": case.metadata_hash,
                    "case_hash": case.content_hash,
                }
                for case in self.workspace.case_bank.cases
            ],
            "calibration": {
                "content_hash": self.workspace.calibration.content_hash,
                "fixtures": [
                    {
                        "id": fixture.id,
                        "case_id": fixture.case_id,
                        "candidate_response": fixture.candidate_path.read_text(
                            encoding="utf-8"
                        ),
                        "label": fixture.label,
                        "score_range": [
                            fixture.minimum_score,
                            fixture.maximum_score,
                        ],
                    }
                    for fixture in self.workspace.calibration.fixtures
                ],
            },
        }

    def _selected_workspace_configuration(
        self, lock: ExperimentLock
    ) -> str:
        """Project the workspace to only the selected experiment family."""

        payload = dict(load_yaml(self.workspace.repository.workspace_file))
        experiments = _mapping(payload.get("experiments"), "workspace experiments")
        selected_ids = [lock.configuration.id]
        if lock.configuration.based_on is not None:
            selected_ids.append(lock.configuration.based_on)
        payload["experiments"] = {
            experiment_id: experiments[experiment_id] for experiment_id in selected_ids
        }
        stages = _mapping(payload.get("stages"), "workspace stages")
        executor_ids = {
            _string(
                _mapping(stage, f"workspace stage {stage_id}").get("executor"),
                f"workspace stage {stage_id} executor",
            )
            for stage_id, stage in stages.items()
        }
        model_ids: set[str] = set()
        for stage_id, stage in stages.items():
            raw_model = _mapping(stage, f"workspace stage {stage_id}").get("model")
            if raw_model is not None:
                model_ids.add(
                    _string(raw_model, f"workspace stage {stage_id} model")
                )
        executors = _mapping(payload.get("executors"), "workspace executors")
        models = _mapping(payload.get("models"), "workspace models")
        payload["executors"] = {
            executor_id: executors[executor_id]
            for executor_id in sorted(executor_ids)
        }
        payload["models"] = {
            model_id: models[model_id] for model_id in sorted(model_ids)
        }
        workspace = dict(_mapping(payload.get("workspace"), "workspace configuration"))
        workspace["data_root"] = self.artifacts.data_root.as_posix()
        payload["workspace"] = workspace
        return canonical_json_bytes(payload).decode("utf-8")

    def _workspace_file_inventory(self) -> list[Mapping[str, object]]:
        """Store every local file needed to reconstruct a selected revision."""

        repository = self.workspace.repository
        candidates = {
            repository.case_authoring_path,
            repository.capability_catalog_path,
            repository.split_file,
            repository.calibration_root / "manifest.yaml",
            *(fixture.candidate_path for fixture in self.workspace.calibration.fixtures),
        }
        for case in self.workspace.case_bank.cases:
            candidates.update(
                {
                    case.path / "case.yaml",
                    case.solver_prompt_path,
                    case.judge_expected_path,
                    case.judge_rubric_path,
                }
            )
            for root in (
                case.solver_attachments_directory,
                case.expected_artifacts_directory,
            ):
                if root is not None:
                    candidates.update(
                        path
                        for path in root.rglob("*")
                        if path.is_file() and not path.is_symlink()
                    )
        inventory: list[Mapping[str, object]] = []
        workspace_relative = repository.workspace_file.relative_to(
            repository.root
        ).as_posix()
        for path in sorted(candidates):
            resolved = path.resolve()
            if path.is_symlink() or repository.root not in resolved.parents:
                raise IntegrityError(f"revision input file is unsafe: {path}")
            relative = resolved.relative_to(repository.root).as_posix()
            if relative == workspace_relative:
                raise ConfigurationError(
                    "the workspace manifest cannot also be an evaluation input file"
                )
            with resolved.open("rb") as handle:
                reference = self.artifacts.put_blob(handle, "application/octet-stream")
            inventory.append(
                {
                    "path": relative,
                    "artifact": json_value(reference),
                }
            )
        return inventory

    @contextmanager
    def _frozen_coordinator(
        self,
        run: Mapping[str, object],
        environment: Mapping[str, str],
    ):  # type: ignore[no-untyped-def]
        """Rehydrate the immutable experiment definition referenced by a run."""

        run_id = _string(run.get("run_id"), "run id")
        family = _string(run.get("experiment_id"), "run experiment_id")
        revision = _string(
            run.get("experiment_revision_id"), "run experiment_revision_id"
        )
        definition_reference = self._artifact_reference(
            run.get("experiment_definition_artifact"),
            "experiment definition artifact",
        )
        with self.artifacts.get_blob(definition_reference) as handle:
            try:
                loaded = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IntegrityError(
                    f"experiment definition is invalid for run {run_id}"
                ) from error
        definition = _mapping(loaded, "experiment definition")
        if definition.get("schema_version") != 2:
            raise IntegrityError("experiment definition schema_version must be 2")
        if (
            definition.get("experiment_family") != family
            or definition.get("experiment_revision_id") != revision
        ):
            raise IntegrityError("experiment definition identity differs from its run")

        with tempfile.TemporaryDirectory(
            prefix=f"frozen-{revision.removeprefix('sha256:')[:12]}-",
            dir=self.artifacts.temporary_root,
        ) as temporary:
            root = Path(temporary)
            workspace_json = _string(
                definition.get("workspace_configuration_json"),
                "frozen workspace configuration JSON",
            )
            (root / "workspace.yaml").write_text(
                workspace_json,
                encoding="utf-8",
            )
            materialized_paths: set[str] = set()
            for item in _list(definition.get("workspace_files"), "workspace files"):
                entry = _mapping(item, "workspace files[]")
                relative = self._safe_bundle_path(entry.get("path"))
                if relative in materialized_paths:
                    raise IntegrityError(f"duplicate frozen workspace file: {relative}")
                materialized_paths.add(relative)
                reference = self._artifact_reference(
                    entry.get("artifact"), f"workspace file {relative} artifact"
                )
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with self.artifacts.get_blob(reference) as source:
                    destination.write_bytes(source.read())

            repository = WorkspaceRepository.from_file(root / "workspace.yaml")
            snapshot_store = ExternalSnapshotStore(repository.data_root)
            snapshot = self._snapshot_from_hash(
                snapshot_store,
                _string(definition.get("target_snapshot_hash"), "target snapshot hash"),
            )
            context = self._frozen_context(
                _mapping(definition.get("context"), "frozen context"),
                snapshot_store,
            )
            case_bank = CaseBank.discover(repository.cases_root)
            capability_catalog = CapabilityCatalog.load(
                repository.capability_catalog_path
            )
            splits = SplitManifest.load(repository.split_file, case_bank)
            calibration = CalibrationCorpus.load(repository.calibration_root, case_bank)
            validation = _mapping(
                definition.get("validation"), "frozen validation"
            )
            frozen = CompiledWorkspace(
                repository,
                snapshot,
                snapshot_store.directory(snapshot),
                context,
                capability_catalog,
                case_bank,
                splits,
                calibration,
                MappingProxyType(validation),
            )
            frozen_lock = frozen.experiment_lock(family, environment=environment)
            if frozen_lock.content_hash != revision:
                raise IntegrityError(
                    "stored experiment definition does not reproduce its revision"
                )
            yield StageCoordinator(
                frozen,
                lm_factory=self.lm_factory,
                progress_sink=self.progress_sink,
            )

    def _artifact_reference(self, value: object, field: str) -> ArtifactReference:
        payload = _mapping(value, field)
        content_id = _string(payload.get("content_id"), f"{field} content_id")
        media_type = _string(payload.get("media_type"), f"{field} media_type")
        resolved = self.artifacts.reference(content_id, media_type)
        has_storage_metadata = {
            "size_bytes",
            "relative_path",
        }.intersection(payload)
        if has_storage_metadata and has_storage_metadata != {
            "size_bytes",
            "relative_path",
        }:
            raise IntegrityError(f"{field} has incomplete storage metadata")
        if has_storage_metadata and (
            payload["size_bytes"] != resolved.size_bytes
            or payload["relative_path"] != resolved.relative_path
        ):
            raise IntegrityError(f"{field} metadata does not match content storage")
        return resolved

    @staticmethod
    def _safe_bundle_path(value: object) -> str:
        path = PurePosixPath(_string(value, "workspace file path"))
        if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
            raise IntegrityError("workspace file path must be safe and relative")
        return path.as_posix()

    @staticmethod
    def _snapshot_from_hash(
        store: ExternalSnapshotStore, snapshot_hash: str
    ) -> SnapshotLock:
        digest = snapshot_hash.removeprefix("sha256:")
        if (
            not snapshot_hash.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise IntegrityError("snapshot hash must be a sha256 content id")
        lock_file = store.snapshot_root / digest / "snapshot.lock.json"
        if not lock_file.is_file() or lock_file.is_symlink():
            raise IntegrityError(f"immutable snapshot is unavailable: {snapshot_hash}")
        try:
            loaded = json.loads(lock_file.read_text(encoding="utf-8"))
            lock = SnapshotLock.from_mapping(_mapping(loaded, "snapshot lock"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ConfigurationError) as error:
            raise IntegrityError(f"immutable snapshot is invalid: {snapshot_hash}") from error
        if lock.content_hash != snapshot_hash:
            raise IntegrityError("snapshot directory and lock identities differ")
        store.verify(lock)
        return lock

    def _frozen_context(
        self,
        payload: Mapping[str, object],
        store: ExternalSnapshotStore,
    ) -> SnapshotContext:
        raw_sources = _list(payload.get("sources"), "frozen context sources")
        sources: list[SnapshotSourceIdentity] = []
        source_files: dict[str, Path] = {}
        for raw_source in raw_sources:
            item = _mapping(raw_source, "frozen context sources[]")
            include = tuple(
                _string(pattern, "frozen source include[]")
                for pattern in _list(item.get("include"), "frozen source include")
            )
            source = SnapshotSourceIdentity(
                id=_string(item.get("id"), "frozen source id"),
                role=_string(item.get("role"), "frozen source role"),
                repository_url=_string(
                    item.get("repository_url"), "frozen source repository_url"
                ),
                requested_ref=_string(
                    item.get("requested_ref"), "frozen source requested_ref"
                ),
                resolved_commit=_string(
                    item.get("resolved_commit"), "frozen source resolved_commit"
                ),
                snapshot_hash=_string(
                    item.get("snapshot_hash"), "frozen source snapshot_hash"
                ),
                include=include,
            )
            source_lock = self._snapshot_from_hash(store, source.snapshot_hash)
            if (
                source_lock.repository_url != source.repository_url
                or source_lock.resolved_commit != source.resolved_commit
                or source_lock.requested_ref.value != source.requested_ref
            ):
                raise IntegrityError("frozen source identity differs from its snapshot")
            for locked_file in source_lock.files:
                if source.id != "target" and not any(
                    fnmatchcase(locked_file.source_path, pattern) for pattern in include
                ):
                    continue
                qualified = (
                    locked_file.source_path
                    if source.id == "target"
                    else f"references/{source.id}/{locked_file.source_path}"
                )
                if qualified in source_files:
                    raise IntegrityError(f"frozen source path collision: {qualified}")
                source_files[qualified] = (
                    store.directory(source_lock) / locked_file.snapshot_path
                )
            sources.append(source)
        declared_paths = tuple(
            sorted(
                _string(item, "frozen context source_paths[]")
                for item in _list(
                    payload.get("source_paths"), "frozen context source_paths"
                )
            )
        )
        if declared_paths != tuple(sorted(source_files)):
            raise IntegrityError("frozen context source inventory is incomplete")
        skills = _mapping(payload.get("skills"), "frozen context skills")
        return SnapshotContext(
            snapshot_hash=_string(
                payload.get("snapshot_hash"), "frozen context snapshot_hash"
            ),
            resolved_commit=_string(
                payload.get("resolved_commit"), "frozen context resolved_commit"
            ),
            sources=tuple(sources),
            global_context=_text(payload.get("global"), "frozen global context"),
            skill_contexts=MappingProxyType(
                {
                    skill_id: _text(value, f"frozen skill {skill_id}")
                    for skill_id, value in skills.items()
                }
            ),
            source_paths=declared_paths,
            source_files=MappingProxyType(source_files),
        )

    def resume(
        self,
        run_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        through: str | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        if not self._is_active_revision(run, resolved_environment):
            with self._frozen_coordinator(run, resolved_environment) as coordinator:
                return coordinator.resume(
                    run_id,
                    environment=resolved_environment,
                    through=through,
                )
        lock = self.workspace.experiment_lock(experiment_id, environment=resolved_environment)
        self._validate_calibration_split(lock, pilot=pilot)
        self._emit(
            "run_resumed",
            run_id,
            experiment_id,
            details={
                "execution_mode": _execution_mode(run),
                "case_count": self._run_case_count(
                    lock.configuration.mode,
                    pilot=pilot,
                ),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": lock.content_hash,
            },
        )
        return self._advance(
            run_id,
            lock,
            environment=resolved_environment,
            through=through,
            pilot=pilot,
        )

    def prepare_stage(
        self,
        run_id: str,
        stage: StageKind,
        *,
        campaign: str | None = None,
    ) -> tuple[StagePacket, ...]:
        run = self.ledger.load_run(run_id)
        experiment_id = _string(run.get("experiment_id"), "run experiment_id")
        pilot = _execution_mode(run) == "pilot"
        environment = self.workspace.repository.environment()
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.prepare_stage(run_id, stage, campaign=campaign)
        lock = self.workspace.experiment_lock(experiment_id, environment=environment)
        if stage is StageKind.AUTHOR:
            return (self._author_packet(run_id, lock, campaign=campaign),)
        if stage is StageKind.CALIBRATE_JUDGE:
            return (self._calibration_packet(run_id, lock),)
        calibration = self._persisted_packet(run_id, StageKind.CALIBRATE_JUDGE)
        if calibration is None or not self._calibration_passed(calibration):
            raise PreflightError("judge calibration must pass before this stage is prepared")
        if stage is StageKind.OPTIMIZE:
            if pilot:
                raise PreflightError("pilot runs cannot prepare optimization")
            return (self._optimize_packet(run_id, lock, calibration),)
        if stage is StageKind.SOLVE:
            program_dependency: StagePacket | None = None
            selected = None
            if lock.configuration.mode is ExperimentMode.OPTIMIZE:
                program_dependency = self._persisted_packet(run_id, StageKind.OPTIMIZE)
                if program_dependency is None:
                    raise PreflightError("optimize must persist before solve is prepared")
                final = self._persisted_packet(run_id, StageKind.FINAL_EVALUATE)
                selected = "test" if final is not None else "validation"
                if final is not None:
                    program_dependency = final
            selected_splits = (
                ("train", "validation")
                if pilot and lock.configuration.mode is ExperimentMode.EVALUATE
                else None
            )
            return self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split=selected,
                selected_splits=selected_splits,
                program_dependency=program_dependency,
            )
        if stage is StageKind.JUDGE:
            solves = tuple(
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is StageKind.SOLVE and item.state is StageState.PERSISTED
            )
            if not solves:
                raise PreflightError("at least one solve result must persist before judge")
            return self._judge_packets(run_id, lock, calibration, solves)
        if stage is StageKind.FINAL_EVALUATE:
            if pilot:
                raise PreflightError("pilot runs cannot open held-out final evaluation")
            optimize = self._persisted_packet(run_id, StageKind.OPTIMIZE)
            if optimize is None:
                raise PreflightError("optimize must persist before final_evaluate")
            validation = self._persisted_judges(run_id, split="validation")
            if not validation:
                raise PreflightError("validation judgments must persist before final_evaluate")
            return (self._final_packet(run_id, lock, optimize, validation),)
        if stage is StageKind.AGGREGATE:
            judges = self._persisted_judges(run_id)
            if not judges:
                raise PreflightError("judgments must persist before aggregate")
            return (self._aggregate_packet(run_id, lock, calibration, judges),)
        raise ResolutionError(f"unsupported stage: {stage.value}")

    def execute_packet(
        self,
        packet_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        executor_id: str | None = None,
    ) -> StageResult | None:
        packet = self.ledger.load_packet(packet_id)
        run = self.ledger.load_run(packet.run_id)
        resolved_environment = (
            environment if environment is not None else self.workspace.repository.environment()
        )
        if not self._is_active_revision(run, resolved_environment):
            with self._frozen_coordinator(run, resolved_environment) as coordinator:
                return coordinator.execute_packet(
                    packet_id,
                    environment=resolved_environment,
                    executor_id=executor_id,
                )
        lock = self.workspace.experiment_lock(
            _string(run.get("experiment_id"), "run experiment_id"),
            environment=resolved_environment,
        )
        session = _ExecutionSession(
            self,
            lock,
            resolved_environment,
        )
        result = self._run_packet(packet, lock, session, executor_id=executor_id)
        if self._packet_ends_run(packet, result):
            self._finalize_run(packet.run_id, packet=packet)
        return result

    def submit_file(
        self,
        packet_id: str,
        result_file: Path,
        *,
        executor_id: str,
        worker_id: str,
        lease_id: str,
    ) -> StageResult:
        packet = self.ledger.load_packet(packet_id)
        run = self.ledger.load_run(packet.run_id)
        environment = self.workspace.repository.environment()
        if not self._is_active_revision(run, environment):
            with self._frozen_coordinator(run, environment) as coordinator:
                return coordinator.submit_file(
                    packet_id,
                    result_file,
                    executor_id=executor_id,
                    worker_id=worker_id,
                    lease_id=lease_id,
                )
        if not result_file.is_file() or result_file.is_symlink():
            raise ResolutionError(f"stage result file does not exist: {result_file}")
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ConfigurationError("stage result file must contain one JSON object")
        if executor_id != packet.executor.id:
            raise IntegrityError("submission executor does not match packet executor")
        output = self.ledger.put_json("result", payload)
        now = _timestamp()
        result = StageResult.create(
            packet=packet,
            status="completed",
            executor_version=None,
            worker_id=worker_id,
            outputs=(output,),
            usage={},
            provenance={
                "cache_eligible": not (
                    packet.stage is StageKind.CALIBRATE_JUDGE
                    and payload.get("passed") is not True
                )
            },
            started_at=now,
            completed_at=now,
            observed_model=packet.model.id,
        )
        self.ledger.submit(result, lease_id=lease_id)
        self.ledger.validate(packet.packet_id, self._validate_result)
        persisted = self.ledger.persist(packet.packet_id)
        self._emit_packet(
            "packet_completed",
            packet,
            details=self._output_event_details(packet.stage, payload),
        )
        if self._packet_ends_run(packet, persisted):
            self._finalize_run(packet.run_id, packet=packet)
        return persisted

    def _is_active_revision(
        self,
        run: Mapping[str, object],
        environment: Mapping[str, str],
    ) -> bool:
        family = _string(run.get("experiment_id"), "run experiment_id")
        try:
            lock = self.workspace.experiment_lock(family, environment=environment)
        except ResolutionError:
            return False
        return lock.content_hash == run.get("experiment_revision_id")

    def _advance(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        environment: Mapping[str, str],
        through: str | None,
        pilot: bool,
    ) -> RunProgress:
        if through is not None and through not in {item.value for item in StageKind}:
            raise ConfigurationError(f"unknown --through stage: {through}")
        session = _ExecutionSession(self, lock, environment)
        if through == StageKind.AUTHOR.value:
            author = self._author_packet(run_id, lock, campaign=None)
            if not self._ensure_executed(author, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, author)
            return self._progress(run_id, lock.configuration.id, "paused", author.stage)
        calibration = self._calibration_packet(run_id, lock)
        if not self._ensure_executed(calibration, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, calibration)
        if not self._calibration_passed(calibration):
            return self._terminal_progress(
                run_id,
                lock.configuration.id,
                "calibration_failed",
                calibration,
            )
        if through == StageKind.CALIBRATE_JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", calibration.stage)

        program_dependency: StagePacket | None = None
        if lock.configuration.mode is ExperimentMode.OPTIMIZE:
            if pilot:
                raise IntegrityError("pilot execution cannot enter optimization")
            optimize = self._optimize_packet(run_id, lock, calibration)
            if not self._ensure_executed(optimize, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, optimize)
            if through == StageKind.OPTIMIZE.value:
                return self._progress(run_id, lock.configuration.id, "paused", optimize.stage)
            validation_solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split="validation",
                program_dependency=optimize,
            )
            pending = self._execute_all(validation_solves, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            if through == StageKind.SOLVE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
            validation_judges = self._judge_packets(
                run_id, lock, calibration, validation_solves
            )
            pending = self._execute_all(validation_judges, lock, session)
            if pending is not None:
                return self._blocked_progress(run_id, lock.configuration.id, pending)
            if through == StageKind.JUDGE.value:
                return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
            final = self._final_packet(run_id, lock, optimize, validation_judges)
            if not self._ensure_executed(final, lock, session):
                return self._blocked_progress(run_id, lock.configuration.id, final)
            if through == StageKind.FINAL_EVALUATE.value:
                return self._progress(run_id, lock.configuration.id, "paused", final.stage)
            program_dependency = final
            solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_split="test",
                program_dependency=program_dependency,
            )
        else:
            solves = self._solve_packets(
                run_id,
                lock,
                calibration,
                selected_splits=("train", "validation") if pilot else None,
            )

        pending = self._execute_all(solves, lock, session)
        if pending is not None:
            return self._blocked_progress(run_id, lock.configuration.id, pending)
        if through == StageKind.SOLVE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.SOLVE)
        judges = self._judge_packets(run_id, lock, calibration, solves)
        pending = self._execute_all(judges, lock, session)
        if pending is not None:
            return self._blocked_progress(run_id, lock.configuration.id, pending)
        if through == StageKind.JUDGE.value:
            return self._progress(run_id, lock.configuration.id, "paused", StageKind.JUDGE)
        all_judges = self._persisted_judges(run_id)
        aggregate = self._aggregate_packet(run_id, lock, calibration, all_judges)
        if not self._ensure_executed(aggregate, lock, session):
            return self._blocked_progress(run_id, lock.configuration.id, aggregate)
        if through == StageKind.AGGREGATE.value:
            return self._progress(run_id, lock.configuration.id, "paused", aggregate.stage)
        aggregate_status = self.ledger.status(aggregate.packet_id)
        assert aggregate_status.result is not None
        result = self.ledger.read_output(aggregate_status.result)
        assert isinstance(result, Mapping)
        return self._terminal_progress(
            run_id,
            lock.configuration.id,
            "pilot_completed" if pilot else "completed",
            aggregate,
            result=result,
        )

    def _execute_all(
        self,
        packets: Sequence[StagePacket],
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> StagePacket | None:
        for position, packet in enumerate(packets, start=1):
            if not self._ensure_executed(
                packet,
                lock,
                session,
                position=position,
                total=len(packets),
            ):
                return packet
        return None

    def _ensure_executed(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        *,
        position: int | None = None,
        total: int | None = None,
    ) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is StageState.PERSISTED:
            return True
        if status.state in {StageState.FAILED, StageState.REJECTED}:
            error = status.result.error.message if status.result and status.result.error else None
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                details={"error": error or status.state.value},
            )
            return False
        with self.ledger.work_lock(packet.work_id):
            cached = self.ledger.reusable_result(packet)
            if cached is not None:
                self._materialize_reused_result(
                    packet,
                    cached,
                    position=position,
                    total=total,
                )
                return True
            return (
                self._run_packet(
                    packet,
                    lock,
                    session,
                    position=position,
                    total=total,
                )
                is not None
            )

    def _materialize_reused_result(
        self,
        packet: StagePacket,
        source: StageResult,
        *,
        position: int | None,
        total: int | None,
    ) -> StageResult:
        """Attach one verified shared result to this run without a model call."""

        source_packet = self.ledger.load_packet(source.packet_id)
        source_output = _mapping(
            self.ledger.read_output(source), "reusable stage output"
        )
        rebound = self._rebind_reused_output(packet, source_output)
        claim = self.ledger.claim(
            packet.packet_id,
            executor_id=packet.executor.id,
            worker_id=f"cache-{uuid4()}",
            lease_seconds=packet.budget.wall_seconds,
        )
        now = _timestamp()
        materialized = StageResult.create(
            packet=packet,
            status="completed",
            executor_version=(
                str(source.executor["version"])
                if source.executor.get("version") is not None
                else None
            ),
            worker_id=claim.worker_id,
            outputs=(self.ledger.put_json("result", rebound),),
            usage={"model_calls": 0, "tokens": 0},
            provenance={
                "cache_hit": True,
                "reuse_policy": packet.reuse_policy,
                "source_work_id": source.work_id,
                "source_result_id": source.result_id,
                "source_attempt_id": source.attempt_id,
                "source_run_id": source_packet.run_id,
                "source_executor": dict(source.executor),
                "source_model": dict(source.model),
                "source_usage": dict(source.usage),
                "artifacts_verified": True,
            },
            started_at=now,
            completed_at=now,
            observed_model=(
                str(source.model["observed_id"])
                if source.model.get("observed_id") is not None
                else packet.model.id
            ),
        )
        self.ledger.submit(materialized, lease_id=claim.lease_id)
        self.ledger.validate(packet.packet_id, self._validate_result)
        persisted = self.ledger.persist(packet.packet_id)
        self._emit_packet(
            "packet_reused",
            packet,
            position=position,
            total=total,
            details={
                "source_result_id": source.result_id,
                "source_run_id": source_packet.run_id,
                **self._output_event_details(packet.stage, rebound),
            },
        )
        return persisted

    def _rebind_reused_output(
        self,
        packet: StagePacket,
        source: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Replace run/report metadata while preserving the cached semantic output."""

        value = dict(source)
        if packet.stage is StageKind.SOLVE:
            current = _mapping(self._input(packet, "case"), "solve input")
            for field in ("case_id", "split", "repetition", "program_hash"):
                value[field] = current[field]
        elif packet.stage is StageKind.JUDGE:
            current = _mapping(self._input(packet, "evaluation"), "judge input")
            value["case_id"] = current["case_id"]
            value["split"] = current["split"]
            value["repetition"] = current["repetition"]
        elif packet.stage is StageKind.CALIBRATE_JUDGE:
            scores = _mapping(value.get("fixture_scores"), "fixture_scores")
            passes = _mapping(value.get("fixture_passes"), "fixture_passes")
            rebound = calibration_result_from_scores(
                self.workspace.calibration,
                {key: float(score) for key, score in scores.items()},
                fixture_passes={key: bool(passed) for key, passed in passes.items()},
            )
            value = dict(json_value(rebound))
        elif packet.stage is StageKind.FINAL_EVALUATE:
            current = _mapping(
                self._input(packet, "publication_gate"), "publication gate"
            )
            value.update(current)
        elif packet.stage is StageKind.AGGREGATE:
            value = dict(self._execute_local(packet, self._lock_for_packet(packet)))
            execution = dict(_mapping(value.get("execution"), "aggregate execution"))
            execution["executed_this_run"] = max(
                0, int(execution.get("executed_this_run", 1)) - 1
            )
            execution["reused_this_run"] = int(
                execution.get("reused_this_run", 0)
            ) + 1
            stages = {
                key: dict(_mapping(item, f"aggregate execution stage {key}"))
                for key, item in _mapping(
                    execution.get("stages", {}), "aggregate execution stages"
                ).items()
            }
            stages["aggregate"] = {
                "executed_this_run": 0,
                "reused_this_run": 1,
            }
            execution["stages"] = stages
            value["execution"] = execution
        return value

    def _lock_for_packet(self, packet: StagePacket) -> ExperimentLock:
        lock = self.workspace.experiment_lock(packet.experiment_id)
        if lock.content_hash != packet.experiment_revision_id:
            raise IntegrityError("packet experiment revision is no longer active")
        return lock

    def _run_packet(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        *,
        executor_id: str | None = None,
        position: int | None = None,
        total: int | None = None,
    ) -> StageResult | None:
        configuration = self._executor(packet)
        selected = executor_id or configuration.id
        if selected != configuration.id:
            raise IntegrityError("executor override does not match immutable packet identity")
        if configuration.adapter == "artifact_exchange":
            self._emit_packet(
                "packet_waiting",
                packet,
                position=position,
                total=total,
            )
            return None
        started_clock = time.monotonic()
        self._emit_packet(
            "packet_started",
            packet,
            position=position,
            total=total,
        )
        claim = self.ledger.claim(
            packet.packet_id,
            executor_id=configuration.id,
            worker_id=f"coordinator-{uuid4()}",
            lease_seconds=packet.budget.wall_seconds,
        )
        started = _timestamp()
        try:
            output = self._automatic_output(packet, lock, session, configuration)
            artifact = self.ledger.put_json("result", output.value)
            provenance = {**dict(output.provenance), **dict(session.evidence(packet.packet_id))}
            if (
                packet.stage is StageKind.CALIBRATE_JUDGE
                and output.value.get("passed") is not True
            ):
                provenance["cache_eligible"] = False
            result = StageResult.create(
                packet=packet,
                status="completed",
                executor_version=output.version or configuration.version,
                worker_id=claim.worker_id,
                outputs=(artifact,),
                usage=output.usage,
                provenance=provenance,
                started_at=started,
                completed_at=_timestamp(),
                observed_model=output.observed_model,
            )
        except HarnessExecutionPending:
            self._emit_packet(
                "packet_waiting",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
            )
            return None
        except AutomaticExecutionFailed as error:
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=error.version or configuration.version,
                worker_id=claim.worker_id,
                usage=error.usage,
                provenance=error.provenance,
                started_at=started,
                completed_at=_timestamp(),
                observed_model=error.observed_model or packet.model.id,
                error=StageError(
                    error.kind,
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={"error": failed.error.message if failed.error else error.kind},
            )
            return None
        except Exception as error:
            failed = StageResult.create(
                packet=packet,
                status="failed",
                executor_version=configuration.version,
                worker_id=claim.worker_id,
                usage=session.usage(packet.packet_id),
                provenance=session.evidence(packet.packet_id),
                started_at=started,
                completed_at=_timestamp(),
                observed_model=packet.model.id,
                error=StageError(
                    type(error).__name__.lower(),
                    self._redacted_error(error, session.environment),
                ),
            )
            self.ledger.submit(failed, lease_id=claim.lease_id)
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={"error": failed.error.message if failed.error else str(error)},
            )
            return None
        self.ledger.submit(result, lease_id=claim.lease_id)
        try:
            self.ledger.validate(packet.packet_id, self._validate_result)
        except Exception as error:
            self._emit_packet(
                "packet_failed",
                packet,
                position=position,
                total=total,
                duration_seconds=time.monotonic() - started_clock,
                details={"error": str(error)},
            )
            return None
        persisted = self.ledger.persist(packet.packet_id)
        self._emit_packet(
            "packet_completed",
            packet,
            position=position,
            total=total,
            duration_seconds=time.monotonic() - started_clock,
            details=self._output_event_details(packet.stage, output.value),
        )
        return persisted

    def _packet_ends_run(
        self,
        packet: StagePacket,
        result: StageResult | None,
    ) -> bool:
        status = self.ledger.status(packet.packet_id)
        if result is None or status.state is not StageState.PERSISTED:
            return False
        if packet.stage is StageKind.AGGREGATE:
            return True
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            output = _mapping(self.ledger.read_output(result), "calibration output")
            return output.get("passed") is False
        return False

    def _finalize_run(
        self,
        run_id: str,
        *,
        packet: StagePacket | None = None,
    ) -> PublishedReport:
        report = collect_artifact_garbage(
            self.artifacts,
            delete=True,
        )
        blobs = _mapping(report.get("blobs"), "garbage collection blobs")
        temporary = _mapping(report.get("temporary"), "garbage collection temporary")
        cleanup_details = {
            "deleted_blobs": blobs.get("deleted_count", 0),
            "deleted_blob_bytes": blobs.get("deleted_bytes", 0),
            "deleted_temporary": temporary.get("deleted_count", 0),
            "deleted_temporary_bytes": temporary.get("deleted_bytes", 0),
        }
        if packet is not None:
            self._emit_packet("cleanup_completed", packet, details=cleanup_details)
        published = self.build_report(run_id, replace=True)
        run = self.ledger.load_run(run_id)
        self._emit(
            "report_completed",
            run_id,
            _string(run.get("experiment_id"), "run experiment_id"),
            stage=None if packet is None else packet.stage.value,
            packet_id=None if packet is None else packet.packet_id,
            details=dict(published.as_dict()),
        )
        return published

    def _blocked_progress(
        self,
        run_id: str,
        experiment_id: str,
        packet: StagePacket,
    ) -> RunProgress:
        return self._progress(run_id, experiment_id, "awaiting_execution", packet.stage)

    def _terminal_progress(
        self,
        run_id: str,
        experiment_id: str,
        status: str,
        packet: StagePacket,
        *,
        result: Mapping[str, object] | None = None,
    ) -> RunProgress:
        try:
            published = self._finalize_run(run_id, packet=packet)
        except AgentEvalError as error:
            return self._progress(
                run_id,
                experiment_id,
                "report_failed",
                packet.stage,
                result={
                    **dict(result or {}),
                    "report_error": str(error),
                },
            )
        return self._progress(
            run_id,
            experiment_id,
            status,
            packet.stage,
            result={
                **dict(result or {}),
                "report": dict(published.as_dict()),
            },
        )

    def _emit_packet(
        self,
        kind: str,
        packet: StagePacket,
        *,
        position: int | None = None,
        total: int | None = None,
        duration_seconds: float | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        context = self._packet_event_context(packet)
        context_details = context.pop("details", {})
        assert isinstance(context_details, Mapping)
        self._emit(
            kind,
            packet.run_id,
            packet.experiment_id,
            stage=packet.stage.value,
            packet_id=packet.packet_id,
            position=position,
            total=total,
            model=packet.model.id,
            executor=packet.executor.id,
            duration_seconds=duration_seconds,
            details={**context_details, **dict(details or {})},
            **context,
        )

    def _emit(
        self,
        kind: str,
        run_id: str,
        experiment_id: str,
        *,
        stage: str | None = None,
        packet_id: str | None = None,
        work_id: str | None = None,
        attempt_id: str | None = None,
        experiment_revision_id: str | None = None,
        case_id: str | None = None,
        case_title: str | None = None,
        skill: str | None = None,
        split: str | None = None,
        repetition: int | None = None,
        position: int | None = None,
        total: int | None = None,
        model: str | None = None,
        executor: str | None = None,
        duration_seconds: float | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self.progress_sink is None:
            return
        self.progress_sink(
            ProgressEvent(
                kind=kind,
                run_id=run_id,
                experiment_id=experiment_id,
                stage=stage,
                packet_id=packet_id,
                work_id=work_id,
                attempt_id=attempt_id,
                experiment_revision_id=experiment_revision_id,
                case_id=case_id,
                case_title=case_title,
                skill=skill,
                split=split,
                repetition=repetition,
                position=position,
                total=total,
                model=model,
                executor=executor,
                duration_seconds=duration_seconds,
                details=dict(details or {}),
            )
        )

    def _packet_event_context(self, packet: StagePacket) -> dict[str, object]:
        input_id = {
            StageKind.SOLVE: "case",
            StageKind.JUDGE: "evaluation",
        }.get(packet.stage)
        if input_id is not None:
            value = _mapping(self._input(packet, input_id), f"{packet.stage.value} input")
            case_id = _string(value.get("case_id"), f"{packet.stage.value} case_id")
            case = next(
                (item for item in self.workspace.case_bank.cases if item.id == case_id),
                None,
            )
            return {
                "case_id": case_id,
                "case_title": None if case is None else case.title,
                "skill": value.get("skill") if isinstance(value.get("skill"), str) else (
                    None if case is None else case.skill
                ),
                "split": value.get("split") if isinstance(value.get("split"), str) else None,
                "repetition": (
                    value.get("repetition")
                    if isinstance(value.get("repetition"), int)
                    and not isinstance(value.get("repetition"), bool)
                    else None
                ),
            }
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            value = _mapping(self._input(packet, "calibration"), "calibration input")
            fixtures = value.get("fixtures")
            return {
                "details": {
                    "fixture_count": len(fixtures) if isinstance(fixtures, list) else 0
                }
            }
        if packet.stage is StageKind.OPTIMIZE:
            value = _mapping(self._input(packet, "training_set"), "optimization input")
            cases = value.get("cases")
            return {
                "details": {"training_case_count": len(cases) if isinstance(cases, list) else 0}
            }
        return {}

    def _run_case_count(self, mode: ExperimentMode, *, pilot: bool) -> int:
        if not pilot and mode is ExperimentMode.EVALUATE:
            return len(self.workspace.case_bank.cases)
        selected_splits = (
            {"train", "validation"}
            if pilot
            else {"train", "validation", "test"}
        )
        return sum(
            self.workspace.splits.split_for(case) in selected_splits
            for case in self.workspace.case_bank.cases
        )

    @staticmethod
    def _output_event_details(
        stage: StageKind,
        output: Mapping[str, object],
    ) -> Mapping[str, object]:
        if stage is StageKind.AUTHOR:
            packages = output.get("candidate_packages")
            if isinstance(packages, Sequence) and not isinstance(packages, (str, bytes)):
                case_ids = [
                    item.get("id")
                    for item in packages
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                ]
                return {"produced_count": len(packages), "case_ids": case_ids}
        if stage is StageKind.CALIBRATE_JUDGE:
            return {"passed": output.get("passed") is True}
        if stage is StageKind.JUDGE:
            return {
                "score": output.get("score"),
                "passed": output.get("passed"),
            }
        if stage is StageKind.AGGREGATE:
            return {
                key: output[key]
                for key in ("case_count", "mean_score", "pass_rate", "diagnostic_only")
                if key in output
            }
        if stage in {StageKind.OPTIMIZE, StageKind.FINAL_EVALUATE}:
            return {"published": output.get("published") is True}
        return {}

    def _automatic_output(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
        executor: ExecutorConfiguration,
    ) -> ExecutorOutput:
        if executor.adapter == "codex_cli":
            from ms_agent_eval.adapters.codex_cli import CodexCliExecutor

            return CodexCliExecutor(self.workspace, lock, executor).execute(
                packet, self.ledger
            )
        if executor.adapter == "local":
            return ExecutorOutput(
                self._execute_local(packet, lock), {}, executor.version, None
            )
        if executor.adapter != "openai_compatible":
            raise ConfigurationError(f"unsupported executor adapter: {executor.adapter}")
        if packet.stage is StageKind.AUTHOR:
            return self._execute_author(packet, session)
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            return self._execute_calibration(packet, lock, session)
        if packet.stage is StageKind.SOLVE:
            return self._execute_solve(packet, lock, session)
        if packet.stage is StageKind.JUDGE:
            return self._execute_judge(packet, lock, session)
        raise ConfigurationError(
            f"openai_compatible cannot automatically execute {packet.stage.value}"
        )

    def _execute_author(
        self, packet: StagePacket, session: _ExecutionSession
    ) -> ExecutorOutput:
        authoring_context = _mapping(self._input(packet, "authoring_context"), "author input")
        program = create_author_program()
        lm, observer = session.lm("author", packet)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="author",
                program_hash=program_hash(program),
                inputs={
                    "authoring_context": json.dumps(
                        authoring_context, ensure_ascii=False, sort_keys=True
                    )
                },
                required_outputs=("candidate_packages",),
                primary_output=None,
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed":
            raise IntegrityError(f"author DSPy call failed: {result.error_kind}")
        packages = _list(result.outputs.get("candidate_packages"), "candidate_packages")
        return ExecutorOutput(
            {"candidate_packages": list(packages)},
            _usage(observer.records),
            None,
            packet.model.id,
        )

    def _execute_calibration(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        judge, observer = self._judge(
            lock,
            session,
            packet,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = calibrate_judge(
            judge,
            self.workspace.calibration,
            self.workspace.case_bank,
            self.workspace.context.skill,
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            value,
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_solve(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "case"), "solve input")
        program = create_solver_program()
        compiled = next((item for item in packet.inputs if item.id == "compiled_state"), None)
        if compiled is not None:
            reference = self.artifacts.reference(compiled.content_id, compiled.media_type)
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "state.json"
                with self.artifacts.get_blob(reference) as source:
                    state_file.write_bytes(source.read())
                load_state_json(program, state_file)
        lm, observer = session.lm("solver", packet)
        start = len(observer.records)
        result = self.dspy.execute(
            contract=DspyExecutionContract(
                role="solver",
                program_hash=program_hash(program),
                inputs={
                    "global_context": values["global_context"],
                    "skill_context": values["skill_context"],
                    "task": values["task"],
                },
                required_outputs=("response",),
                primary_output="response",
            ),
            program=program,
            lm=lm,
            observer=observer,
        )
        if result.status != "completed" or result.primary_response is None:
            raise IntegrityError(f"solver DSPy call failed: {result.error_kind}")
        return ExecutorOutput(
            {
                "case_id": values["case_id"],
                "split": values["split"],
                "repetition": values["repetition"],
                "response": result.primary_response,
                "program_hash": program_hash(program),
                "call_ids": [item.call_id for item in result.calls],
            },
            _usage(observer.records[start:]),
            None,
            lock.models["solver"].model,
        )

    def _execute_judge(
        self,
        packet: StagePacket,
        lock: ExperimentLock,
        session: _ExecutionSession,
    ) -> ExecutorOutput:
        values = _mapping(self._input(packet, "evaluation"), "judge input")
        case_id = _string(values.get("case_id"), "judge case_id")
        case = next((item for item in self.workspace.case_bank.cases if item.id == case_id), None)
        if case is None:
            raise IntegrityError(f"judge packet references unknown case {case_id!r}")
        judge, observer = self._judge(
            lock,
            session,
            packet,
            repetitions=self._judge_repetitions(packet.run_id),
        )
        start = len(observer.records)
        result = judge.evaluate(
            case,
            _string(values.get("candidate_response"), "candidate response"),
            skill_context=_string(values.get("skill_context"), "skill context"),
        )
        value = json_value(result)
        assert isinstance(value, Mapping)
        return ExecutorOutput(
            {**value, "split": values["split"], "repetition": values["repetition"]},
            _usage(observer.records[start:]),
            None,
            lock.models["judge"].model,
        )

    def _execute_local(
        self, packet: StagePacket, lock: ExperimentLock
    ) -> Mapping[str, object]:
        if packet.stage is StageKind.OPTIMIZE:
            experiment = lock.configuration
            if experiment.optimizer is None:
                raise IntegrityError("optimization stage has no optimizer")
            by_split = self._cases_by_split()
            for split in ("train", "validation", "test"):
                if not by_split[split]:
                    raise PreflightError(f"optimization requires a non-empty {split} split")
            raw_k = experiment.optimizer.parameters.get("k", 2)
            raw_seed = experiment.optimizer.parameters.get("seed", 0)
            if not isinstance(raw_k, int) or isinstance(raw_k, bool) or raw_k < 1:
                raise PreflightError("LabeledFewShot k must be a positive integer")
            if not isinstance(raw_seed, int) or isinstance(raw_seed, bool):
                raise PreflightError("LabeledFewShot seed must be an integer")
            train = [
                dspy.Example(
                    global_context=self.workspace.context.global_context,
                    skill_context=self.workspace.context.skill(case.skill),
                    task=case.prompt,
                    response=case.expected_response,
                ).with_inputs("global_context", "skill_context", "task")
                for case in by_split["train"]
            ]
            previous = random.getstate()
            try:
                random.seed(raw_seed)
                compiled = dspy.LabeledFewShot(k=raw_k).compile(
                    create_solver_program(), trainset=train, sample=True
                )
            finally:
                random.setstate(previous)
            state = self.dspy.save_state_artifact(compiled)
            return {
                "state_artifact": json_value(state),
                "compiled_program_hash": program_hash(compiled),
                "train_case_hash": content_hash(
                    tuple((case.id, case.content_hash) for case in by_split["train"])
                ),
                "validation_assignment_hash": content_hash(
                    tuple((case.id, case.content_hash) for case in by_split["validation"])
                ),
                "state_format": "json",
                "published": True,
            }
        if packet.stage is StageKind.FINAL_EVALUATE:
            gate = _mapping(
                self._input(packet, "publication_gate"), "publication gate"
            )
            return {
                "published": True,
                **dict(gate),
            }
        if packet.stage is StageKind.AGGREGATE:
            cases = []
            graph_nodes = []
            execution_by_stage: dict[str, Counter[str]] = {}
            role_usage: dict[str, Counter[str]] = {
                "solver": Counter(),
                "judge": Counter(),
            }
            for status in self.ledger.list(packet.run_id):
                if status.state is not StageState.PERSISTED or status.result is None:
                    continue
                graph_nodes.append(
                    {
                        "stage": status.packet.stage.value,
                        "work_id": status.packet.work_id,
                        "result_id": status.result.result_id,
                    }
                )
                disposition = (
                    "reused_this_run"
                    if status.result.provenance.get("cache_hit") is True
                    else "executed_this_run"
                )
                execution_by_stage.setdefault(
                    status.packet.stage.value, Counter()
                )[disposition] += 1
                role = None
                if status.packet.stage is StageKind.SOLVE:
                    role = "solver"
                elif status.packet.stage in {StageKind.CALIBRATE_JUDGE, StageKind.JUDGE}:
                    role = "judge"
                if status.packet.stage is StageKind.JUDGE:
                    evaluation = _mapping(
                        self.ledger.read_output(status.result), "judge output"
                    )
                    if (
                        lock.configuration.mode is not ExperimentMode.OPTIMIZE
                        or evaluation.get("split") == "test"
                    ):
                        cases.append(dict(evaluation))
                if role is not None:
                    for key, value in status.result.usage.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            role_usage[role][key] += value
            workspace = self.workspace.repository.workspace
            run = self.ledger.load_run(packet.run_id)
            execution_mode = _execution_mode(run)
            value: dict[str, object] = {
                "schema_version": 2,
                "run_id": packet.run_id,
                "experiment_id": packet.experiment_id,
                "lock_hash": lock.content_hash,
                "run_graph_id": content_hash(
                    {
                        "experiment_revision_id": lock.content_hash,
                        "nodes": sorted(
                            graph_nodes,
                            key=lambda item: (
                                str(item["stage"]),
                                str(item["work_id"]),
                                str(item["result_id"]),
                            ),
                        ),
                        "terminal_work_id": packet.work_id,
                    }
                ),
                "mode": lock.mode,
                "execution_mode": execution_mode,
                "benchmark_valid": execution_mode == "benchmark",
                "coverage_status": run.get("coverage_status", "unknown"),
                "evaluated_splits": sorted(
                    {
                        str(item["split"])
                        for item in cases
                        if isinstance(item.get("split"), str)
                    }
                ),
                "cases": cases,
                "case_count": len(cases),
                "role_usage": {
                    role: dict(values) for role, values in role_usage.items()
                },
                "execution": {
                    "executed_this_run": sum(
                        values["executed_this_run"]
                        for values in execution_by_stage.values()
                    )
                    + 1,
                    "reused_this_run": sum(
                        values["reused_this_run"]
                        for values in execution_by_stage.values()
                    ),
                    "stages": {
                        **{
                            stage: dict(values)
                            for stage, values in sorted(execution_by_stage.items())
                        },
                        "aggregate": {
                            "executed_this_run": 1,
                            "reused_this_run": 0,
                        },
                    },
                },
                "independent": (
                    workspace.stages["solve"].executor
                    != workspace.stages["judge"].executor
                ),
            }
            if execution_mode == "benchmark":
                value["mean_score"] = (
                    sum(float(item["score"]) for item in cases) / len(cases)
                    if cases
                    else 0.0
                )
                value["pass_rate"] = (
                    sum(bool(item["passed"]) for item in cases) / len(cases)
                    if cases
                    else 0.0
                )
            else:
                value["diagnostic_only"] = True
                value["benchmark_metrics"] = None
            return value
        raise ConfigurationError(f"local executor cannot execute {packet.stage.value}")

    def _judge(
        self,
        lock: ExperimentLock,
        session: _ExecutionSession,
        packet: StagePacket,
        *,
        repetitions: int | None = None,
    ) -> tuple[LlmJudge, ModelCallObserver]:
        lm, observer = session.lm("judge", packet)
        program = create_judge_program()

        def observed(**inputs: object) -> Mapping[str, object]:
            result = self.dspy.execute(
                contract=DspyExecutionContract(
                    role="judge",
                    program_hash=program_hash(program),
                    inputs=inputs,
                    required_outputs=("criterion_scores", "hard_failures", "feedback"),
                    primary_output=None,
                ),
                program=program,
                lm=lm,
                observer=observer,
            )
            if result.status != "completed":
                raise IntegrityError(f"judge DSPy call failed: {result.error_kind}")
            return {**result.outputs, "call_ids": [item.call_id for item in result.calls]}

        return (
            LlmJudge(
                observed,
                model_hash=lock.models["judge"].content_hash,
                program_hash=lock.judge_program_hash,
                repetitions=(
                    repetitions
                    if repetitions is not None
                    else self.workspace.repository.workspace.evaluation.judge.repetitions
                ),
            ),
            observer,
        )

    def _judge_repetitions(self, run_id: str) -> int:
        return (
            1
            if _execution_mode(self.ledger.load_run(run_id)) == "pilot"
            else self.workspace.repository.workspace.evaluation.judge.repetitions
        )

    def _author_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        *,
        campaign: str | None,
    ) -> StagePacket:
        value = {
            "authoring_instructions": self.workspace.repository.case_authoring_instructions(),
            "capability_catalog": json_value(self.workspace.capability_catalog),
            "global_context": self.workspace.context.global_context,
            "skill_contexts": dict(self.workspace.context.skill_contexts),
            "source_evidence": self.workspace.context.supporting_source(
                "case-authoring", campaign or "complete capability coverage"
            ),
            "source_snapshot_hash": self.workspace.context.snapshot_hash,
            "campaign": campaign or "Author the next complete, grounded case campaign.",
        }
        return self._packet(
            run_id,
            lock,
            StageKind.AUTHOR,
            inputs=(self.ledger.put_json("authoring_context", value),),
            instructions={
                "task": "Produce candidate case packages only; review and promotion remain explicit.",
                "output": {"candidate_packages": "list[case-package]"},
            },
            dependencies=(),
            output_schema="authored-cases-v1",
        )

    def _calibration_packet(self, run_id: str, lock: ExperimentLock) -> StagePacket:
        cases = {item.id: item for item in self.workspace.case_bank.cases}
        fixtures = []
        semantic_fixtures = []
        for fixture in self.workspace.calibration.fixtures:
            case = cases[fixture.case_id]
            candidate_response = fixture.candidate_path.read_text(encoding="utf-8")
            judge_case = self._judge_case_input(case)
            fixtures.append(
                {
                    "id": fixture.id,
                    "candidate_response": candidate_response,
                    "case": judge_case,
                }
            )
            semantic_fixtures.append(
                {
                    "id": fixture.id,
                    "candidate_response_hash": content_hash(candidate_response),
                    "judge_view": {
                        key: value
                        for key, value in judge_case.items()
                        if key not in {"case_id", "case_hash"}
                    },
                }
            )
        calibration_input = self.ledger.put_json(
            "calibration",
            {
                "corpus_hash": self.workspace.calibration.content_hash,
                "fixtures": fixtures,
            },
        )
        return self._packet(
            run_id,
            lock,
            StageKind.CALIBRATE_JUDGE,
            inputs=(calibration_input,),
            instructions={
                "task": "Judge every locked fixture and report whether all calibration checks pass.",
                "output": "CalibrationResult",
            },
            dependencies=(),
            output_schema="judge-calibration-v1",
            work_spec={
                "fixtures": semantic_fixtures,
                "judge_program_hash": lock.judge_program_hash,
                "repetitions": self._judge_repetitions(run_id),
            },
        )

    def _optimize_packet(
        self, run_id: str, lock: ExperimentLock, calibration: StagePacket
    ) -> StagePacket:
        train = [
            {
                "case_id": case.id,
                "split": "train",
                "global_context": self.workspace.context.global_context,
                "skill_context": self.workspace.context.skill(case.skill),
                "task": case.prompt,
                "expected_response": case.expected_response,
            }
            for case in self._cases_by_split()["train"]
        ]
        return self._packet(
            run_id,
            lock,
            StageKind.OPTIMIZE,
            inputs=(self.ledger.put_json("training_set", {"cases": train}),),
            instructions={
                "task": "Compile and publish a state-only DSPy artifact from train cases.",
                "optimizer": json_value(lock.configuration.optimizer),
                "forbidden_splits": ["test", "challenge"],
            },
            dependencies=(calibration.packet_id,),
            output_schema="compiled-program-v1",
            work_dependencies=(),
        )

    def _solve_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        *,
        selected_split: str | None = None,
        selected_splits: Sequence[str] | None = None,
        program_dependency: StagePacket | None = None,
    ) -> tuple[StagePacket, ...]:
        if selected_split is not None and selected_splits is not None:
            raise IntegrityError("solve selection cannot use singular and plural splits")
        split_selection = (
            None
            if selected_split is None and selected_splits is None
            else frozenset(
                (selected_split,) if selected_split is not None else selected_splits or ()
            )
        )
        if split_selection is not None and not split_selection:
            raise IntegrityError("solve split selection must not be empty")
        if split_selection is not None and not split_selection <= {
            "train",
            "validation",
            "test",
            "challenge",
        }:
            raise IntegrityError("solve split selection contains an unknown split")
        state_artifact = None
        solver_program_hash = lock.solver_program_hash
        dependencies = [calibration.packet_id]
        if program_dependency is not None:
            dependencies.append(program_dependency.packet_id)
            optimize = self._persisted_packet(run_id, StageKind.OPTIMIZE)
            if optimize is None:
                raise IntegrityError("compiled program dependency is missing")
            status = self.ledger.status(optimize.packet_id)
            assert status.result is not None
            output = _mapping(self.ledger.read_output(status.result), "optimize output")
            state = _mapping(output.get("state_artifact"), "compiled state artifact")
            solver_program_hash = _string(
                output.get("compiled_program_hash"), "compiled program hash"
            )
            state_artifact = StageArtifact(
                "compiled_state",
                _string(state.get("content_id"), "compiled state content_id"),
                _string(state.get("media_type"), "compiled state media_type"),
            )
        packets = []
        reuse = self.workspace.repository.workspace.stages["solve"].reuse
        repetitions = (
            1
            if selected_split is not None
            else (
                reuse.samples
                if reuse.policy.value == "replicate"
                else lock.configuration.repetitions
            )
        )
        for case in self.workspace.case_bank.cases:
            split = self.workspace.splits.split_for(case)
            if split_selection is not None and split not in split_selection:
                continue
            for repetition in range(1, repetitions + 1):
                inputs = [
                    self.ledger.put_json(
                        "case",
                        {
                            "case_id": case.id,
                            "case_hash": case.content_hash,
                            "skill": case.skill,
                            "split": split,
                            "repetition": repetition,
                            "global_context": self.workspace.context.global_context,
                            "skill_context": self.workspace.context.skill(case.skill),
                            "task": case.prompt,
                            "solver_attachments": dict(case.solver_attachments()),
                            "program_hash": solver_program_hash,
                        },
                    )
                ]
                if state_artifact is not None:
                    inputs.append(state_artifact)
                solver_view = {
                    "case_solver_view_hash": case.solver_view_hash,
                    "global_context": self.workspace.context.global_context,
                    "skill_context": self.workspace.context.skill(case.skill),
                    "task": case.prompt,
                    "solver_attachments": dict(case.solver_attachments()),
                    "program_hash": solver_program_hash,
                    "compiled_state": (
                        None if state_artifact is None else state_artifact.content_id
                    ),
                    "runtime": content_hash(
                        self.workspace.repository.workspace.runtime(lock.configuration.id)
                    ),
                    **(
                        {"sample_slot": repetition}
                        if reuse.policy.value != "replicate"
                        and lock.configuration.repetitions > 1
                        else {}
                    ),
                }
                packets.append(
                    self._packet(
                        run_id,
                        lock,
                        StageKind.SOLVE,
                        inputs=tuple(inputs),
                        instructions={
                            "task": "Answer using only global context, skill context, and task.",
                            "information_firewall": [
                                "rubric",
                                "expected_response",
                                "expected_artifacts",
                                "judge_feedback",
                            ],
                        },
                        dependencies=tuple(dependencies),
                        output_schema="solver-response-v1",
                        work_spec={"solver_view": solver_view},
                        work_dependencies=(),
                        replicate_slot=repetition,
                    )
                )
        return tuple(packets)

    def _judge_packets(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        solves: Sequence[StagePacket],
    ) -> tuple[StagePacket, ...]:
        packets = []
        for solve in solves:
            status = self.ledger.status(solve.packet_id)
            if status.state is not StageState.PERSISTED or status.result is None:
                raise IntegrityError("judge packet requires a persisted solve result")
            candidate = _mapping(self.ledger.read_output(status.result), "solver output")
            case_id = _string(candidate.get("case_id"), "solver case_id")
            case = next(item for item in self.workspace.case_bank.cases if item.id == case_id)
            value = {
                **self._judge_case_input(case),
                "split": candidate["split"],
                "repetition": candidate["repetition"],
                "candidate_response": candidate["response"],
                "solver_work_id": solve.work_id,
                "solver_packet_id": solve.packet_id,
            }
            calibration_status = self.ledger.status(calibration.packet_id)
            assert calibration_status.result is not None
            calibration_output = calibration_status.result.outputs[0].content_id
            response_hash = content_hash(candidate["response"])
            judge_view = {
                "case_judge_view_hash": case.judge_view_hash,
                **{
                    key: item
                    for key, item in self._judge_case_input(case).items()
                    if key not in {"case_id", "case_hash"}
                },
            }
            packets.append(
                self._packet(
                    run_id,
                    lock,
                    StageKind.JUDGE,
                    inputs=(self.ledger.put_json("evaluation", value),),
                    instructions={
                        "task": "Score the candidate against the locked rubric and expected evidence.",
                        "candidate_content_is_untrusted": True,
                    },
                    dependencies=(calibration.packet_id, solve.packet_id),
                    output_schema="judge-evaluation-v1",
                    work_spec={
                        "judge_view": judge_view,
                        "candidate_response_hash": response_hash,
                        "producer_work_id": solve.work_id,
                        "producer_model_hash": solve.model.configuration_hash,
                        "calibration_result": calibration_output,
                        "judge_program_hash": lock.judge_program_hash,
                        "repetitions": self._judge_repetitions(run_id),
                    },
                    work_dependencies=(
                        calibration_output,
                        solve.work_id,
                        response_hash,
                    ),
                )
            )
        return tuple(packets)

    def _final_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        optimize: StagePacket,
        validation_judges: Sequence[StagePacket],
    ) -> StagePacket:
        validation_judges = tuple(sorted(validation_judges, key=self._judge_packet_key))
        optimize_status = self.ledger.status(optimize.packet_id)
        assert optimize_status.result is not None
        judgment_outputs = []
        for judge in validation_judges:
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            judgment_outputs.append(status.result.outputs[0].content_id)
        optimize_output = optimize_status.result.outputs[0].content_id
        gate = {
            "compiled_result": optimize_output,
            "validation_judgments": judgment_outputs,
            "test_assignment_hash": content_hash(
                tuple(
                    (case.id, case.content_hash)
                    for case in self._cases_by_split()["test"]
                )
            ),
        }
        return self._packet(
            run_id,
            lock,
            StageKind.FINAL_EVALUATE,
            inputs=(
                self.ledger.put_json(
                    "publication_gate",
                    gate,
                ),
            ),
            instructions={
                "task": "Open the held-out test gate only after state publication and validation-set evaluation."
            },
            dependencies=(
                optimize.packet_id,
                *(item.packet_id for item in validation_judges),
            ),
            output_schema="final-evaluation-gate-v1",
            work_spec={"publication_gate": gate},
            work_dependencies=(optimize_output, *judgment_outputs),
        )

    def _aggregate_packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        calibration: StagePacket,
        judges: Sequence[StagePacket],
    ) -> StagePacket:
        judges = tuple(sorted(judges, key=self._judge_packet_key))
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        judgment_outputs = []
        for judge in judges:
            status = self.ledger.status(judge.packet_id)
            assert status.result is not None
            judgment_outputs.append(status.result.outputs[0].content_id)
        evaluation_index = {
            "judgment_results": judgment_outputs,
            "execution_mode": execution_mode,
            "benchmark_valid": execution_mode == "benchmark",
        }
        return self._packet(
            run_id,
            lock,
            StageKind.AGGREGATE,
            inputs=(
                self.ledger.put_json(
                    "evaluation_index",
                    evaluation_index,
                ),
            ),
            instructions={
                "task": (
                    "Persist diagnostic judgments without benchmark metrics."
                    if execution_mode == "pilot"
                    else "Aggregate validated judgments deterministically."
                )
            },
            dependencies=(calibration.packet_id, *(item.packet_id for item in judges)),
            output_schema="run-summary-v2",
            work_spec={
                "evaluation_index": evaluation_index,
                "aggregation_implementation": "run-summary-v2",
            },
            work_dependencies=tuple(judgment_outputs),
        )

    def _judge_packet_key(self, packet: StagePacket) -> tuple[str, str, int]:
        value = _mapping(self._input(packet, "evaluation"), "judge input")
        return (
            _string(value.get("case_id"), "judge case_id"),
            _string(value.get("split"), "judge split"),
            int(value.get("repetition", 0)),
        )

    def _packet(
        self,
        run_id: str,
        lock: ExperimentLock,
        stage: StageKind,
        *,
        inputs: Sequence[StageArtifact],
        instructions: Mapping[str, object],
        dependencies: Sequence[str],
        output_schema: str,
        work_spec: Mapping[str, object] | None = None,
        work_dependencies: Sequence[str] | None = None,
        replicate_slot: int = 1,
    ) -> StagePacket:
        workspace = self.workspace.repository.workspace
        configuration = workspace.stages[stage.value]
        executor = workspace.executors[configuration.executor]
        model = resolve_stage_model(
            workspace,
            stage.value,
            role="judge" if stage in {StageKind.CALIBRATE_JUDGE, StageKind.JUDGE} else None,
        )
        dependency_artifacts: tuple[str, ...]
        if work_dependencies is not None:
            dependency_artifacts = tuple(work_dependencies)
        else:
            dependency_artifacts = tuple(
                output.content_id
                for dependency in dependencies
                for output in (
                    self.ledger.status(dependency).result.outputs
                    if self.ledger.status(dependency).result is not None
                    else ()
                )
            )
        packet = StagePacket.create(
            run_id=run_id,
            experiment_id=lock.configuration.id,
            experiment_revision_id=lock.content_hash,
            stage=stage,
            attempt=1,
            replicate_slot=replicate_slot,
            reuse_policy=configuration.reuse.policy.value,
            dependencies=dependencies,
            dependency_artifacts=dependency_artifacts,
            work_spec=(
                work_spec
                if work_spec is not None
                else {"inputs": [item.content_id for item in inputs]}
            ),
            inputs=inputs,
            instructions=self.ledger.put_json("instructions", instructions),
            output_schema=output_schema,
            executor=StageExecutorIdentity(
                executor.id, executor.adapter, executor.execution_hash
            ),
            model=StageModelIdentity(
                None if model is None else model.model,
                None if model is None else model.content_hash,
            ),
            budget=StageBudget(
                configuration.budget.model_calls,
                configuration.budget.tokens,
                configuration.budget.wall_seconds,
            ),
        )
        self._validate_packet(packet)
        matching_attempts = [
            item.packet
            for item in self.ledger.list(run_id)
            if self._same_stage_work(item.packet, packet)
        ]
        if matching_attempts:
            return max(matching_attempts, key=lambda item: item.attempt)
        return self.ledger.prepare(packet)

    @staticmethod
    def _same_stage_work(left: StagePacket, right: StagePacket) -> bool:
        """Match retry attempts while excluding their attempt-derived packet ids."""

        return (
            left.run_id == right.run_id
            and left.experiment_id == right.experiment_id
            and left.experiment_revision_id == right.experiment_revision_id
            and left.work_id == right.work_id
            and left.replicate_slot == right.replicate_slot
            and left.dependencies == right.dependencies
            and left.inputs == right.inputs
            and left.budget == right.budget
        )

    def _validate_packet(self, packet: StagePacket) -> None:
        if packet.stage is StageKind.SOLVE:
            if {item.id for item in packet.inputs} - {"case", "compiled_state"}:
                raise IntegrityError("solve packet contains an unauthorized input")
            forbidden = {
                "rubric",
                "expected_response",
                "expected_artifacts",
                "candidate_response",
                "judge_feedback",
            }
            if _contains_key(self._input(packet, "case"), forbidden):
                raise IntegrityError("solve packet crosses the hidden judging-material firewall")
        if packet.stage is StageKind.CALIBRATE_JUDGE:
            forbidden = {"label", "score_range", "minimum_score", "maximum_score"}
            if _contains_key(self._input(packet, "calibration"), forbidden):
                raise IntegrityError(
                    "calibration packet exposes coordinator-only labels or score ranges"
                )
        if packet.stage is StageKind.OPTIMIZE:
            if _contains_value(self._input(packet, "training_set"), {"test", "challenge"}):
                raise IntegrityError("optimization packet contains a held-out split")

    def _validate_result(self, packet: StagePacket, result: StageResult) -> None:
        if result.status != "completed":
            raise IntegrityError("failed result cannot be validated")
        self._validate_result_budget(packet, result)
        if len(result.outputs) != 1 or result.outputs[0].id != "result":
            raise IntegrityError("stage result must contain exactly one result output")
        payload = _mapping(self.ledger.read_output(result), f"{packet.output_schema} output")
        if packet.output_schema == "authored-cases-v1":
            _list(payload.get("candidate_packages"), "candidate_packages")
        elif packet.output_schema == "judge-calibration-v1":
            if not isinstance(payload.get("passed"), bool):
                raise IntegrityError("judge calibration output requires boolean passed")
            scores = _mapping(payload.get("fixture_scores"), "fixture_scores")
            passes = _mapping(payload.get("fixture_passes"), "fixture_passes")
            calibration = _mapping(self._input(packet, "calibration"), "calibration input")
            corpus_hash = _string(calibration.get("corpus_hash"), "calibration corpus_hash")
            if payload.get("corpus_hash") != corpus_hash:
                raise IntegrityError("judge calibration output has the wrong corpus hash")
            expected_ids = {item.id for item in self.workspace.calibration.fixtures}
            if set(scores) != expected_ids or set(passes) != expected_ids:
                raise IntegrityError("judge calibration must score every fixture exactly")
            for fixture_id, score in scores.items():
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise IntegrityError("judge calibration scores must be numeric")
                if not 0 <= float(score) <= 1:
                    raise IntegrityError("judge calibration scores must be in [0, 1]")
                if not isinstance(passes[fixture_id], bool):
                    raise IntegrityError("judge calibration pass decisions must be boolean")
            expected_result = calibration_result_from_scores(
                self.workspace.calibration,
                {key: float(value) for key, value in scores.items()},
                fixture_passes={key: bool(value) for key, value in passes.items()},
            )
            if payload.get("passed") is not expected_result.passed:
                raise IntegrityError("judge calibration pass decision was not deterministic")
            if payload.get("content_hash") != expected_result.content_hash:
                raise IntegrityError("judge calibration result identity hash is invalid")
        elif packet.output_schema == "solver-response-v1":
            for field in ("case_id", "split", "response", "program_hash"):
                _string(payload.get(field), f"solver response {field}")
            if not isinstance(payload.get("repetition"), int):
                raise IntegrityError("solver response repetition must be an integer")
            case_input = _mapping(self._input(packet, "case"), "solve case input")
            for field in ("case_id", "split", "repetition", "program_hash"):
                if payload.get(field) != case_input.get(field):
                    raise IntegrityError(f"solver response {field} does not match its packet")
        elif packet.output_schema == "judge-evaluation-v1":
            _string(payload.get("case_id"), "judge evaluation case_id")
            score = payload.get("score")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0 <= score <= 1
            ):
                raise IntegrityError("judge evaluation score must be in [0, 1]")
            if not isinstance(payload.get("passed"), bool):
                raise IntegrityError("judge evaluation passed must be boolean")
            _list(payload.get("votes"), "judge evaluation votes")
        elif packet.output_schema == "compiled-program-v1":
            if payload.get("state_format") != "json" or payload.get("published") is not True:
                raise IntegrityError("compiled program must be published state-only JSON")
            state = _mapping(payload.get("state_artifact"), "state_artifact")
            self.artifacts.reference(
                _string(state.get("content_id"), "state artifact content_id"),
                _string(state.get("media_type"), "state artifact media_type"),
            )
        elif packet.output_schema == "final-evaluation-gate-v1":
            if payload.get("published") is not True:
                raise IntegrityError("final evaluation gate must be published")
            _string(payload.get("test_assignment_hash"), "test assignment hash")
        elif packet.output_schema == "run-summary-v2":
            _list(payload.get("cases"), "run summary cases")
            _string(payload.get("run_graph_id"), "run summary graph id")
            if payload.get("run_id") != packet.run_id:
                raise IntegrityError("run summary is bound to another run")
        else:
            raise IntegrityError(f"unknown output schema: {packet.output_schema}")

    @staticmethod
    def _validate_result_budget(packet: StagePacket, result: StageResult) -> None:
        for key, limit in (
            ("model_calls", packet.budget.model_calls),
            ("tokens", packet.budget.tokens),
        ):
            value = result.usage.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise IntegrityError(f"stage result usage {key} must be non-negative")
            if value > limit:
                raise IntegrityError(f"stage result exceeds its {key} budget")
        try:
            started = datetime.fromisoformat(_string(result.timing.get("started_at"), "started_at"))
            completed = datetime.fromisoformat(
                _string(result.timing.get("completed_at"), "completed_at")
            )
        except ValueError as error:
            raise IntegrityError("stage result timing must use ISO-8601 timestamps") from error
        duration = (completed - started).total_seconds()
        if duration < 0:
            raise IntegrityError("stage result completion precedes its start")
        if duration > packet.budget.wall_seconds:
            raise IntegrityError("stage result exceeds its wall_seconds budget")

    def _judge_case_input(self, case) -> Mapping[str, object]:  # type: ignore[no-untyped-def]
        return {
            "case_id": case.id,
            "case_hash": case.content_hash,
            "task": case.prompt,
            "skill_context": self.workspace.context.skill(case.skill),
            "rubric": case.rubric.prompt_text(),
            "expected_response": case.expected_response,
            "expected_artifacts": json.dumps(
                case.expected_artifacts(), ensure_ascii=False, sort_keys=True
            ),
        }

    def _input(self, packet: StagePacket, input_id: str) -> object:
        artifact = next((item for item in packet.inputs if item.id == input_id), None)
        if artifact is None:
            raise IntegrityError(f"packet has no {input_id!r} input")
        reference = self.artifacts.reference(artifact.content_id, artifact.media_type)
        with self.artifacts.get_blob(reference) as handle:
            return json.load(handle)

    def _executor(self, packet: StagePacket) -> ExecutorConfiguration:
        workspace = self.workspace.repository.workspace
        try:
            executor = workspace.executors[packet.executor.id]
        except KeyError as error:
            raise IntegrityError("packet executor is no longer configured") from error
        if executor.execution_hash != packet.executor.configuration_hash:
            raise IntegrityError("packet executor configuration has changed")
        return executor

    def _budget_ledger(self, stage: StageKind) -> BudgetLedger:
        budget: RoleBudget = self.workspace.repository.workspace.stages[stage.value].budget
        return BudgetLedger(
            BudgetLimits(
                model_calls=budget.model_calls,
                configured_cost=float("inf"),
                tokens=budget.tokens,
                wall_seconds=budget.wall_seconds,
                concurrency=1,
            )
        )

    def _persisted_packet(self, run_id: str, stage: StageKind) -> StagePacket | None:
        return next(
            (
                item.packet
                for item in self.ledger.list(run_id)
                if item.packet.stage is stage and item.state is StageState.PERSISTED
            ),
            None,
        )

    def _persisted_judges(
        self, run_id: str, *, split: str | None = None
    ) -> tuple[StagePacket, ...]:
        values = []
        for item in self.ledger.list(run_id):
            if item.packet.stage is not StageKind.JUDGE or item.state is not StageState.PERSISTED:
                continue
            if split is not None:
                assert item.result is not None
                output = _mapping(self.ledger.read_output(item.result), "judge output")
                if output.get("split") != split:
                    continue
            values.append(item.packet)
        return tuple(values)

    def _calibration_passed(self, packet: StagePacket) -> bool:
        status = self.ledger.status(packet.packet_id)
        if status.state is not StageState.PERSISTED or status.result is None:
            return False
        result = _mapping(self.ledger.read_output(status.result), "calibration output")
        return result.get("passed") is True

    def _cases_by_split(self):  # type: ignore[no-untyped-def]
        values = {key: [] for key in ("train", "validation", "test", "challenge")}
        for case in self.workspace.case_bank.cases:
            values[self.workspace.splits.split_for(case)].append(case)
        return {key: tuple(items) for key, items in values.items()}

    def _progress(
        self,
        run_id: str,
        experiment_id: str,
        status: str,
        stage: StageKind | None,
        *,
        result: Mapping[str, object] | None = None,
    ) -> RunProgress:
        run = self.ledger.load_run(run_id)
        execution_mode = _execution_mode(run)
        packets = tuple(
            {
                "packet_id": item.packet.packet_id,
                "work_id": item.packet.work_id,
                "attempt_id": item.packet.attempt_id,
                "stage": item.packet.stage.value,
                "attempt": item.packet.attempt,
                "state": item.state.value,
                "executor": item.packet.executor.id,
                "model": item.packet.model.id,
                "reused": (
                    item.result is not None
                    and item.result.provenance.get("cache_hit") is True
                ),
                "source_result_id": (
                    None
                    if item.result is None
                    else item.result.provenance.get("source_result_id")
                ),
                **{
                    key: value
                    for key, value in self._packet_event_context(item.packet).items()
                    if key != "details" and value is not None
                },
            }
            for item in self.ledger.list(run_id)
        )
        progress = RunProgress(
            3,
            run_id,
            experiment_id,
            execution_mode,
            execution_mode == "benchmark",
            str(run.get("coverage_status", "unknown")),
            status,
            None if stage is None else stage.value,
            packets,
            result,
        )
        self._emit(
            "run_status",
            run_id,
            experiment_id,
            stage=None if stage is None else stage.value,
            details={
                "status": status,
                "coverage_status": str(run.get("coverage_status", "unknown")),
                "execution_mode": execution_mode,
                "result": dict(result or {}),
                "data_root": self.artifacts.data_root.as_posix(),
                "experiment_revision_id": run.get("experiment_revision_id"),
            },
        )
        return progress

    def _validate_calibration_split(
        self, lock: ExperimentLock, *, pilot: bool = False
    ) -> None:
        if lock.configuration.mode is not ExperimentMode.OPTIMIZE and not pilot:
            return
        cases = {case.id: case for case in self.workspace.case_bank.cases}
        held_out = sorted(
            fixture.id
            for fixture in self.workspace.calibration.fixtures
            if self.workspace.splits.split_for(cases[fixture.case_id])
            in {"test", "challenge"}
        )
        if held_out:
            raise PreflightError(
                "optimization and pilot judge calibration cannot use held-out "
                "test/challenge cases: "
                f"{held_out}"
            )

    @staticmethod
    def _redacted_error(error: Exception, environment: Mapping[str, str]) -> str:
        message = str(error)
        for key, value in environment.items():
            if ("KEY" in key or "TOKEN" in key or "SECRET" in key) and value:
                message = message.replace(value, "[REDACTED]")
        return message


def _usage(records: Sequence) -> Mapping[str, object]:  # type: ignore[type-arg]
    completed = [item for item in records if item.status == "completed"]
    failed = [item for item in records if item.status == "failed"]
    return {
        "model_calls": len(records),
        "completed_calls": len(completed),
        "failed_calls": len(failed),
        "tokens": sum(
            int(item.usage.get("total_tokens", 0) or 0)
            for item in completed
            if isinstance(item.usage.get("total_tokens", 0), int)
        ),
        "configured_cost": round(sum(item.configured_cost for item in completed), 10),
        "error_kinds": dict(Counter(item.error_kind for item in failed)),
    }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _execution_mode(run: Mapping[str, object]) -> str:
    value = run.get("execution_mode")
    if value is None and run.get("schema_version") == 1:
        return "benchmark"
    if value not in {"benchmark", "pilot"}:
        raise IntegrityError("run execution_mode must be benchmark or pilot")
    expected_validity = value == "benchmark"
    if run.get("benchmark_valid") is not expected_validity:
        raise IntegrityError("run benchmark_valid contradicts its execution_mode")
    if value == "pilot" and run.get("selected_splits") != ["train", "validation"]:
        raise IntegrityError("pilot run must be restricted to train/validation splits")
    if value == "pilot" and run.get("judge_repetitions") != 1:
        raise IntegrityError("pilot run must use exactly one diagnostic judge repetition")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise IntegrityError(f"{field} must be a list")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{field} must be a non-empty string")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IntegrityError(f"{field} must be a string")
    return value


def _contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return bool(set(map(str, value)) & keys) or any(
            _contains_key(item, keys) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_key(item, keys) for item in value)
    return False


def _contains_value(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, str):
        return value in forbidden
    if isinstance(value, Mapping):
        return any(_contains_value(item, forbidden) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_value(item, forbidden) for item in value)
    return False
