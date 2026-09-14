from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from .errors import ConfigurationError, IntegrityError
from .hashing import canonical_json_bytes, sha256_file
from .models import ArtifactReference


def validate_external_data_root(data_root: Path, *, workspace_root: Path | None = None) -> Path:
    resolved = data_root.resolve()
    if workspace_root is not None:
        workspace = workspace_root.resolve()
        if resolved == workspace or workspace in resolved.parents:
            raise ConfigurationError("runtime artifacts must remain outside the Git workspace")
    return resolved


class ArtifactStore(Protocol):
    def put_blob(self, content: BinaryIO, media_type: str) -> ArtifactReference: ...

    def get_blob(self, reference: ArtifactReference) -> BinaryIO: ...

    def put_manifest(self, key: str, document: Mapping[str, object]) -> ArtifactReference: ...

    def verify(self, reference: ArtifactReference) -> bool: ...


def _safe_manifest_key(key: str) -> str:
    path = PurePosixPath(key)
    if not key or path.is_absolute() or ".." in path.parts or "\x00" in key or ":" in path.parts[0]:
        raise ConfigurationError("manifest key must be a safe relative POSIX path")
    return path.as_posix().removesuffix(".json")


class FilesystemArtifactStore:
    def __init__(self, data_root: Path, *, workspace_root: Path | None = None) -> None:
        self.data_root = validate_external_data_root(data_root, workspace_root=workspace_root)
        self.operations_root = self.data_root / "operations"
        self.summary_root = self.data_root / "summary"
        self.blob_root = self.operations_root / "blobs" / "sha256"
        self.manifest_root = self.operations_root / "manifests"
        self.temporary_root = self.operations_root / "tmp"
        for directory in (
            self.blob_root,
            self.manifest_root,
            self.temporary_root,
            self.summary_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def put_blob(self, content: BinaryIO, media_type: str) -> ArtifactReference:
        if not media_type.strip():
            raise ConfigurationError("artifact media type must not be empty")
        descriptor, temporary_name = tempfile.mkstemp(prefix="blob-", dir=self.temporary_root)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while chunk := content.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            hex_digest = digest.hexdigest()
            destination = self.blob_root / hex_digest
            if destination.exists():
                os.unlink(temporary_name)
            else:
                os.replace(temporary_name, destination)
            reference = ArtifactReference(
                f"sha256:{hex_digest}",
                media_type,
                size,
                destination.relative_to(self.data_root).as_posix(),
            )
            if not self.verify(reference):
                raise IntegrityError("content-addressed blob failed verification")
            return reference
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def get_blob(self, reference: ArtifactReference) -> BinaryIO:
        if not self.verify(reference):
            raise IntegrityError(f"artifact failed verification: {reference.content_id}")
        return (self.data_root / reference.relative_path).open("rb")

    def reference(self, content_id: str, media_type: str) -> ArtifactReference:
        """Resolve a content id without trusting a caller-provided mutable path."""

        prefix = "sha256:"
        digest = content_id.removeprefix(prefix)
        if (
            not content_id.startswith(prefix)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ConfigurationError("artifact content id must be a sha256 digest")
        path = self.blob_root / digest
        if not path.is_file() or path.is_symlink():
            raise IntegrityError(f"content-addressed artifact does not exist: {content_id}")
        reference = ArtifactReference(
            content_id,
            media_type,
            path.stat().st_size,
            path.relative_to(self.data_root).as_posix(),
        )
        if not self.verify(reference):
            raise IntegrityError(f"artifact failed verification: {content_id}")
        return reference

    def put_manifest(self, key: str, document: Mapping[str, object]) -> ArtifactReference:
        safe_key = _safe_manifest_key(key)
        payload = canonical_json_bytes(document)
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.manifest_root / f"{safe_key}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != payload:
                raise IntegrityError(f"immutable manifest key already contains other data: {key}")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        reference = ArtifactReference(
            f"sha256:{digest}",
            "application/json",
            len(payload),
            destination.relative_to(self.data_root).as_posix(),
        )
        if not self.verify(reference):
            raise IntegrityError("manifest failed verification")
        return reference

    def verify(self, reference: ArtifactReference) -> bool:
        relative = PurePosixPath(reference.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = self.data_root / relative.as_posix()
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == reference.size_bytes
            and sha256_file(path) == reference.content_id
        )
