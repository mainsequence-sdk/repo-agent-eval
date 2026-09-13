from __future__ import annotations

import fcntl
import json
import re
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .errors import ConfigurationError, IntegrityError
from .storage import FilesystemArtifactStore


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_ID = re.compile(r"^sha256:([0-9a-f]{64})$")


def collect_artifact_garbage(
    store: FilesystemArtifactStore,
    *,
    delete: bool = False,
    minimum_age_seconds: float = 3600,
    clock: Callable[[], float] = time.time,
) -> Mapping[str, object]:
    """Find or delete artifacts unreachable from every durable manifest.

    Manifests are the roots of the artifact graph. JSON blobs may point to
    additional blobs, so reachability is followed recursively. A minimum age
    protects files produced by an execution that has not published its
    manifest yet.
    """

    if (
        not isinstance(minimum_age_seconds, (int, float))
        or isinstance(minimum_age_seconds, bool)
        or minimum_age_seconds < 0
    ):
        raise ConfigurationError("garbage collection minimum age must be non-negative")

    lock_path = store.operations_root / "stage-ledger.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _collect_locked(
                store,
                delete=delete,
                minimum_age_seconds=float(minimum_age_seconds),
                now=float(clock()),
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _collect_locked(
    store: FilesystemArtifactStore,
    *,
    delete: bool,
    minimum_age_seconds: float,
    now: float,
) -> Mapping[str, object]:
    managed: dict[str, Path] = {}
    unmanaged: list[str] = []
    for path in sorted(store.blob_root.iterdir()):
        if path.is_symlink() or not path.is_file() or _DIGEST.fullmatch(path.name) is None:
            unmanaged.append(path.name)
            continue
        managed[path.name] = path

    reachable = _reachable_blob_ids(store, set(managed))
    unreferenced = set(managed) - reachable
    eligible = {
        digest
        for digest in unreferenced
        if now - managed[digest].stat().st_mtime >= minimum_age_seconds
    }
    protected_recent = unreferenced - eligible

    eligible_bytes = sum(managed[digest].stat().st_size for digest in eligible)
    unreferenced_bytes = sum(managed[digest].stat().st_size for digest in unreferenced)
    total_bytes = sum(path.stat().st_size for path in managed.values())
    deleted_blob_count = 0
    deleted_blob_bytes = 0
    if delete:
        for digest in sorted(eligible):
            path = managed[digest]
            size = path.stat().st_size
            path.unlink()
            deleted_blob_count += 1
            deleted_blob_bytes += size

    temporary_entries = _temporary_entries(store)
    eligible_temporary = [
        path
        for path in temporary_entries
        if now - _latest_mtime(path) >= minimum_age_seconds
    ]
    temporary_bytes = sum(_entry_size(path) for path in temporary_entries)
    eligible_temporary_bytes = sum(_entry_size(path) for path in eligible_temporary)
    deleted_temporary_count = 0
    deleted_temporary_bytes = 0
    if delete:
        for path in eligible_temporary:
            size = _entry_size(path)
            _remove_entry(path)
            deleted_temporary_count += 1
            deleted_temporary_bytes += size

    sample = sorted(eligible)[:20]
    return {
        "schema_version": 1,
        "mode": "delete" if delete else "dry_run",
        "data_root": store.data_root.as_posix(),
        "minimum_age_seconds": minimum_age_seconds,
        "blobs": {
            "total_count": len(managed),
            "total_bytes": total_bytes,
            "reachable_count": len(reachable),
            "unreferenced_count": len(unreferenced),
            "unreferenced_bytes": unreferenced_bytes,
            "eligible_count": len(eligible),
            "eligible_bytes": eligible_bytes,
            "protected_recent_count": len(protected_recent),
            "deleted_count": deleted_blob_count,
            "deleted_bytes": deleted_blob_bytes,
            "eligible_content_id_sample": [f"sha256:{digest}" for digest in sample],
            "sample_truncated": len(eligible) > len(sample),
            "unmanaged_entries": unmanaged,
        },
        "temporary": {
            "total_count": len(temporary_entries),
            "total_bytes": temporary_bytes,
            "eligible_count": len(eligible_temporary),
            "eligible_bytes": eligible_temporary_bytes,
            "deleted_count": deleted_temporary_count,
            "deleted_bytes": deleted_temporary_bytes,
        },
    }


def _reachable_blob_ids(store: FilesystemArtifactStore, managed: set[str]) -> set[str]:
    roots: set[str] = set()
    for path in sorted(store.manifest_root.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise IntegrityError(f"artifact manifest is not a regular file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrityError(f"cannot garbage-collect with an invalid manifest: {path}") from error
        roots.update(_content_ids(payload))

    reachable: set[str] = set()
    pending = [digest for digest in roots if digest in managed]
    while pending:
        digest = pending.pop()
        if digest in reachable:
            continue
        reachable.add(digest)
        path = store.blob_root / digest
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        for child in _content_ids(payload):
            if child in managed and child not in reachable:
                pending.append(child)
    return reachable


def _content_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "content_id" and isinstance(item, str):
                match = _CONTENT_ID.fullmatch(item)
                if match is not None:
                    found.add(match.group(1))
            found.update(_content_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_content_ids(item))
    return found


def _temporary_entries(store: FilesystemArtifactStore) -> list[Path]:
    entries = list(store.temporary_root.iterdir())
    entries.extend(
        path
        for path in store.manifest_root.rglob(".*.tmp")
        if path.is_file() or path.is_symlink()
    )
    return sorted(entries)


def _entry_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    return sum(
        item.lstat().st_size
        for item in path.rglob("*")
        if item.is_symlink() or item.is_file()
    )


def _latest_mtime(path: Path) -> float:
    latest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for item in path.rglob("*"):
            latest = max(latest, item.lstat().st_mtime)
    return latest


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)
