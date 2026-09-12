from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType

from .errors import IntegrityError, ResolutionError
from .hashing import content_hash
from .models import SnapshotLock
from .workspace import AuthoringSourceConfiguration


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
class SnapshotContext:
    snapshot_hash: str
    resolved_commit: str
    sources: tuple[SnapshotSourceIdentity, ...]
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


def load_snapshot_context(
    lock: SnapshotLock,
    snapshot_directory: Path,
    *,
    global_paths: Sequence[str] | None = None,
    authoring_snapshots: Sequence[
        tuple[AuthoringSourceConfiguration, SnapshotLock, Path]
    ] = (),
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
    skills = {
        item.unit_id: (snapshot_directory / item.snapshot_path).read_text(encoding="utf-8")
        for item in lock.units
    }
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
    snapshot_hash = (
        lock.content_hash
        if not authoring_snapshots
        else content_hash({"schema_version": 1, "sources": source_identity})
    )
    return SnapshotContext(
        snapshot_hash=snapshot_hash,
        resolved_commit=lock.resolved_commit,
        sources=source_identity,
        global_context="\n\n".join(global_parts),
        skill_contexts=skills,
        source_paths=tuple(sorted(source_files)),
        source_files=MappingProxyType(source_files),
    )
