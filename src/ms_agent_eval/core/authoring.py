from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import yaml

from .errors import ConfigurationError, IntegrityError, ResolutionError
from .hashing import content_hash, sha256_file
from .models import SnapshotLock
from .workspace import AuthoringSourceConfiguration, SkillImportConfiguration


@dataclass(frozen=True)
class SnapshotSourceIdentity:
    id: str
    role: str
    repository_url: str
    requested_ref: str
    resolved_commit: str
    snapshot_hash: str
    include: tuple[str, ...]


@dataclass(frozen=True)
class InstructionUnitIdentity:
    id: str
    source_id: str
    source_type: str
    source_path: str
    content_hash: str


@dataclass(frozen=True)
class InstructionImportIdentity:
    id: str
    source: str
    note: str
    manifest_path: str
    manifest_hash: str
    source_metadata: Mapping[str, object]
    units: tuple[InstructionUnitIdentity, ...]


@dataclass(frozen=True)
class SnapshotContext:
    snapshot_hash: str
    resolved_commit: str
    sources: tuple[SnapshotSourceIdentity, ...]
    instruction_imports: tuple[InstructionImportIdentity, ...]
    instruction_units: tuple[InstructionUnitIdentity, ...]
    global_context: str
    skill_contexts: Mapping[str, str]
    source_paths: tuple[str, ...]
    source_files: Mapping[str, Path]
    workspace_source_paths: tuple[str, ...]

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
        """Select bounded evidence text without deciding evaluation semantics."""

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
                    candidates = sorted(
                        candidates, key=lambda item: (-item[0], item[1])
                    )[:200]
        source_manifest = "# Immutable authoring sources\n\n" + "\n".join(
            "- "
            f"{source.id}: role={source.role}; repository={source.repository_url}; "
            f"ref={source.requested_ref}; commit={source.resolved_commit}; "
            f"snapshot={source.snapshot_hash}; include={','.join(source.include)}"
            for source in self.sources
        )
        parts: list[str] = [source_manifest]
        size = len(source_manifest)
        for _, source_path, value in sorted(
            candidates, key=lambda item: (-item[0], item[1])
        ):
            section = f"# Source: {source_path}\n\n{value}\n"
            if size + len(section) > maximum_characters:
                remaining = maximum_characters - size
                if remaining > len(source_path) + 10:
                    parts.append(section[:remaining])
                break
            parts.append(section)
            size += len(section)
        return "\n".join(parts)


def _mapping(value: object, field: str, *, path: Path) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{field} must be a mapping", path=path)
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, field: str, *, path: Path) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field} must be a sequence", path=path)
    return value


def _text(value: object, field: str, *, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string", path=path)
    return value.strip()


def _reject_unknown(
    payload: Mapping[str, object],
    allowed: set[str],
    field: str,
    *,
    path: Path,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unknown fields: {unknown}", path=path)


def _safe_workspace_file(root: Path, relative: str, *, field: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ResolutionError(f"{field} contains a symlink: {relative}")
    if not candidate.is_file():
        raise ResolutionError(f"{field} does not exist: {relative}")
    resolved = candidate.resolve()
    if root.resolve() not in resolved.parents:
        raise ResolutionError(f"{field} escapes the experiment workspace: {relative}")
    return candidate


def _selected_import_files(root: Path, configuration: SkillImportConfiguration) -> tuple[str, ...]:
    selection = configuration.skills
    if selection.directory is None:
        return selection.files
    directory = root.joinpath(*PurePosixPath(selection.directory).parts)
    if directory.is_symlink() or not directory.is_dir():
        raise ResolutionError(
            f"instruction import {configuration.id!r} directory does not exist or is unsafe: "
            f"{selection.directory}"
        )
    if root.resolve() not in directory.resolve().parents:
        raise ResolutionError(
            f"instruction import {configuration.id!r} directory escapes the experiment workspace"
        )
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted(directory.rglob("SKILL.md"))
        if path.is_file() and not path.is_symlink()
    )


def _load_instruction_import(
    root: Path,
    configuration: SkillImportConfiguration,
) -> tuple[
    InstructionImportIdentity,
    Mapping[str, str],
    Mapping[str, Path],
]:
    manifest_path = _safe_workspace_file(
        root,
        configuration.manifest,
        field=f"instruction import {configuration.id!r} manifest",
    )
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(
            f"instruction import {configuration.id!r} manifest is invalid: {error}",
            path=manifest_path,
        ) from error
    manifest = _mapping(payload, "instruction import manifest", path=manifest_path)
    _reject_unknown(
        manifest,
        {"schema_version", "id", "source", "skills"},
        "instruction import manifest",
        path=manifest_path,
    )
    if manifest.get("schema_version") != 1:
        raise ConfigurationError(
            "instruction import manifest schema_version must be 1",
            path=manifest_path,
        )
    if _text(manifest.get("id"), "instruction import manifest id", path=manifest_path) != configuration.id:
        raise ConfigurationError(
            "instruction import manifest id differs from workspace configuration",
            path=manifest_path,
        )
    source = _mapping(
        manifest.get("source"),
        "instruction import manifest source",
        path=manifest_path,
    )
    if _text(source.get("type"), "instruction import source type", path=manifest_path) != configuration.source:
        raise ConfigurationError(
            "instruction import manifest source type differs from workspace configuration",
            path=manifest_path,
        )
    selected_paths = set(_selected_import_files(root, configuration))
    raw_units = _sequence(
        manifest.get("skills"),
        "instruction import manifest skills",
        path=manifest_path,
    )
    units: list[InstructionUnitIdentity] = []
    skills: dict[str, str] = {}
    files: dict[str, Path] = {configuration.manifest: manifest_path}
    manifest_paths: set[str] = set()
    names: set[str] = set()
    uris: set[str] = set()
    for index, raw_unit in enumerate(raw_units):
        field = f"instruction import manifest skills[{index}]"
        unit = _mapping(raw_unit, field, path=manifest_path)
        _reject_unknown(
            unit,
            {"id", "name", "uri", "resource_path", "file", "content_hash"},
            field,
            path=manifest_path,
        )
        unit_id = _text(unit.get("id"), f"{field}.id", path=manifest_path)
        resource_path = _text(
            unit.get("resource_path"), f"{field}.resource_path", path=manifest_path
        )
        safe_resource_path = PurePosixPath(resource_path)
        if (
            safe_resource_path.is_absolute()
            or ".." in safe_resource_path.parts
            or safe_resource_path.name not in {"SKILL.md", "SKILL.markdown"}
        ):
            raise ConfigurationError(
                f"{field}.resource_path must be a safe skill resource path",
                path=manifest_path,
            )
        unit_path = _text(unit.get("file"), f"{field}.file", path=manifest_path)
        safe_unit_path = PurePosixPath(unit_path)
        if (
            safe_unit_path.is_absolute()
            or ".." in safe_unit_path.parts
            or safe_unit_path.name != "SKILL.md"
        ):
            raise ConfigurationError(
                f"{field}.file must be a safe workspace-relative SKILL.md path",
                path=manifest_path,
            )
        name = _text(unit.get("name"), f"{field}.name", path=manifest_path)
        uri = _text(unit.get("uri"), f"{field}.uri", path=manifest_path)
        expected_hash = _text(
            unit.get("content_hash"),
            f"{field}.content_hash",
            path=manifest_path,
        )
        if not expected_hash.startswith("sha256:") or len(expected_hash) != 71:
            raise ConfigurationError(
                f"{field}.content_hash must be a sha256 content id",
                path=manifest_path,
            )
        if unit_id in {item.id for item in units}:
            raise ConfigurationError(f"duplicate imported skill id {unit_id!r}", path=manifest_path)
        if unit_path in manifest_paths:
            raise ConfigurationError(
                f"duplicate imported skill path {unit_path!r}", path=manifest_path
            )
        if name in names or uri in uris:
            raise ConfigurationError(
                "imported skill names and URIs must be unique",
                path=manifest_path,
            )
        skill_path = _safe_workspace_file(
            root,
            unit_path,
            field=f"instruction import {configuration.id!r} skill",
        )
        actual_hash = sha256_file(skill_path)
        if actual_hash != expected_hash:
            raise IntegrityError(
                f"imported skill {unit_id!r} content hash mismatch: "
                f"{expected_hash} != {actual_hash}"
            )
        qualified_id = f"{configuration.id}/{unit_id}"
        units.append(
            InstructionUnitIdentity(
                id=qualified_id,
                source_id=configuration.id,
                source_type=configuration.source,
                source_path=unit_path,
                content_hash=actual_hash,
            )
        )
        skills[qualified_id] = skill_path.read_text(encoding="utf-8")
        files[unit_path] = skill_path
        manifest_paths.add(unit_path)
        names.add(name)
        uris.add(uri)
    if selected_paths != manifest_paths:
        missing = sorted(selected_paths - manifest_paths)
        extra = sorted(manifest_paths - selected_paths)
        raise IntegrityError(
            f"instruction import {configuration.id!r} manifest/file inventory mismatch; "
            f"unlisted={missing}, missing={extra}"
        )
    if not units:
        raise ConfigurationError("instruction import must contain at least one skill", path=manifest_path)
    identity = InstructionImportIdentity(
        id=configuration.id,
        source=configuration.source,
        note=configuration.note,
        manifest_path=configuration.manifest,
        manifest_hash=sha256_file(manifest_path),
        source_metadata=MappingProxyType(dict(source)),
        units=tuple(sorted(units, key=lambda item: item.id)),
    )
    return identity, MappingProxyType(skills), MappingProxyType(files)


def load_snapshot_context(
    lock: SnapshotLock,
    snapshot_directory: Path,
    *,
    global_paths: Sequence[str] | None = None,
    authoring_snapshots: Sequence[
        tuple[AuthoringSourceConfiguration, SnapshotLock, Path]
    ] = (),
    workspace_root: Path | None = None,
    instruction_imports: Sequence[SkillImportConfiguration] = (),
) -> SnapshotContext:
    """Load the immutable evidence Codex may use while authoring cases."""

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
    skills: dict[str, str] = {
        item.unit_id: (snapshot_directory / item.snapshot_path).read_text(encoding="utf-8")
        for item in lock.units
    }
    instruction_units = [
        InstructionUnitIdentity(
            id=item.unit_id,
            source_id=item.source_id,
            source_type="repository",
            source_path=item.source_path,
            content_hash=item.content_hash,
        )
        for item in lock.units
    ]
    source_files = {
        source_path: snapshot_directory / item.snapshot_path
        for source_path, item in files.items()
    }
    identities = [
        SnapshotSourceIdentity(
            id="target",
            role="normative",
            repository_url=lock.repository_url,
            requested_ref=lock.requested_ref.value,
            resolved_commit=lock.resolved_commit,
            snapshot_hash=lock.content_hash,
            include=("**",),
        )
    ]
    for configuration, reference_lock, reference_directory in authoring_snapshots:
        configured_url = configuration.repository.url.removesuffix(".git").removesuffix("/")
        locked_url = reference_lock.repository_url.removesuffix(".git").removesuffix("/")
        if (
            configured_url != locked_url
            or configuration.repository.ref != reference_lock.requested_ref.value
        ):
            raise IntegrityError(
                f"authoring source {configuration.id!r} does not match its snapshot lock"
            )
        identities.append(
            SnapshotSourceIdentity(
                id=configuration.id,
                role=configuration.role,
                repository_url=reference_lock.repository_url,
                requested_ref=reference_lock.requested_ref.value,
                resolved_commit=reference_lock.resolved_commit,
                snapshot_hash=reference_lock.content_hash,
                include=configuration.include,
            )
        )
        for item in reference_lock.files:
            if not any(fnmatchcase(item.source_path, pattern) for pattern in configuration.include):
                continue
            qualified = f"references/{configuration.id}/{item.source_path}"
            if qualified in source_files:
                raise IntegrityError(f"authoring source path collision: {qualified}")
            source_files[qualified] = reference_directory / item.snapshot_path
    source_identity = tuple(identities)
    imported_identities: list[InstructionImportIdentity] = []
    workspace_paths: set[str] = set()
    if instruction_imports and workspace_root is None:
        raise ConfigurationError("workspace_root is required for instruction imports")
    for configuration in instruction_imports:
        assert workspace_root is not None
        imported, imported_skills, imported_files = _load_instruction_import(
            workspace_root,
            configuration,
        )
        collisions = sorted(set(skills).intersection(imported_skills))
        if collisions:
            raise IntegrityError(f"instruction import skill id collisions: {collisions}")
        file_collisions = sorted(set(source_files).intersection(imported_files))
        if file_collisions:
            raise IntegrityError(f"instruction import source path collisions: {file_collisions}")
        imported_identities.append(imported)
        skills.update(imported_skills)
        source_files.update(imported_files)
        workspace_paths.update(imported_files)
        instruction_units.extend(imported.units)
    snapshot_hash = (
        lock.content_hash
        if not authoring_snapshots and not imported_identities
        else content_hash(
            {
                "schema_version": 2,
                "sources": source_identity,
                "instruction_imports": imported_identities,
            }
        )
    )
    return SnapshotContext(
        snapshot_hash=snapshot_hash,
        resolved_commit=lock.resolved_commit,
        sources=source_identity,
        instruction_imports=tuple(imported_identities),
        instruction_units=tuple(sorted(instruction_units, key=lambda item: item.id)),
        global_context="\n\n".join(global_parts),
        skill_contexts=MappingProxyType(skills),
        source_paths=tuple(sorted(source_files)),
        source_files=MappingProxyType(source_files),
        workspace_source_paths=tuple(sorted(workspace_paths)),
    )
