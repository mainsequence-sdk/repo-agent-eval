from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from .models import GitSource, SourceRefKind
from .sources import GitHubSourceProvider, ResolvedSource


@dataclass(frozen=True)
class SkillDirectory:
    path: str
    skill_count: int


@dataclass(frozen=True)
class RepositoryInspection:
    global_instruction_candidates: tuple[str, ...]
    skill_files: tuple[str, ...]
    skill_directories: tuple[SkillDirectory, ...]


@dataclass(frozen=True)
class InspectedRepository:
    resolved: ResolvedSource
    checkout: Path
    inventory: RepositoryInspection


def discover_tags(repository_url: str) -> tuple[str, ...]:
    source = GitSource.from_mapping(
        {
            "type": "github",
            "repository_url": repository_url,
            "ref": {"type": "commit", "value": "0" * 40},
        }
    )
    return GitHubSourceProvider().tags(source)


def inspect_checkout(checkout: Path) -> RepositoryInspection:
    global_candidates = tuple(
        path.relative_to(checkout).as_posix()
        for path in sorted(checkout.rglob("AGENTS.md"))
        if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    )
    skill_files = tuple(
        path.relative_to(checkout).as_posix()
        for path in sorted(checkout.rglob("SKILL.md"))
        if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    )
    counts: Counter[str] = Counter()
    for file in skill_files:
        parent = PurePosixPath(file).parent
        while parent.as_posix() != ".":
            counts[parent.as_posix()] += 1
            parent = parent.parent
    directories = tuple(
        SkillDirectory(path, count)
        for path, count in sorted(
            counts.items(), key=lambda item: (-item[1], len(PurePosixPath(item[0]).parts), item[0])
        )
    )
    return RepositoryInspection(global_candidates, skill_files, directories)


@contextmanager
def inspect_repository(
    repository_url: str,
    repository_ref: str,
    *,
    temporary_root: Path,
) -> Iterator[InspectedRepository]:
    kind = (
        SourceRefKind.COMMIT
        if len(repository_ref) == 40 and all(character in "0123456789abcdef" for character in repository_ref)
        else SourceRefKind.TAG
    )
    source = GitSource.from_mapping(
        {
            "type": "github",
            "repository_url": repository_url,
            "ref": {"type": kind.value, "value": repository_ref},
        }
    )
    provider = GitHubSourceProvider()
    resolved = provider.resolve(source)
    temporary_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="setup-", dir=temporary_root))
    checkout = staging / "repository"
    try:
        provider.materialize(resolved, checkout)
        yield InspectedRepository(resolved, checkout, inspect_checkout(checkout))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
