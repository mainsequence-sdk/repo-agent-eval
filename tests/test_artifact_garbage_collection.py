from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

import pytest

from ms_agent_eval.core.errors import IntegrityError
from ms_agent_eval.core.garbage_collection import collect_artifact_garbage
from ms_agent_eval.core.hashing import canonical_json_bytes
from ms_agent_eval.core.providers import ModelCallObserver
from ms_agent_eval.core.snapshots import ExternalSnapshotStore
from ms_agent_eval.core.storage import FilesystemArtifactStore

def _json_blob(store: FilesystemArtifactStore, value: object):  # type: ignore[no-untyped-def]
    return store.put_blob(BytesIO(canonical_json_bytes(value)), "application/json")


def test_external_data_root_has_only_operations_and_summary(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifacts = FilesystemArtifactStore(root)
    snapshots = ExternalSnapshotStore(root)

    assert {path.name for path in root.iterdir()} == {"operations", "summary"}
    assert artifacts.blob_root == root / "operations" / "blobs" / "sha256"
    assert artifacts.manifest_root == root / "operations" / "manifests"
    assert artifacts.temporary_root == root / "operations" / "tmp"
    assert artifacts.summary_root == root / "summary"
    assert snapshots.snapshot_root == root / "operations" / "snapshots"
    assert snapshots.temporary_root == root / "operations" / "tmp"
    assert snapshots.summary_root == root / "summary"


def test_gc_follows_recursive_references_and_deletes_only_unreachable_artifacts(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    child = _json_blob(store, {"value": "nested"})
    parent = _json_blob(
        store,
        {
            "child": {
                "content_id": child.content_id,
                "media_type": child.media_type,
            }
        },
    )
    garbage = _json_blob(store, {"value": "unreferenced"})
    store.put_manifest(
        "runs/run-1/result",
        {
            "result": {
                "content_id": parent.content_id,
                "media_type": parent.media_type,
            }
        },
    )
    abandoned = store.temporary_root / "abandoned"
    abandoned.mkdir()
    (abandoned / "partial.json").write_text("{}", encoding="utf-8")
    for path in (store.blob_root / garbage.content_id.removeprefix("sha256:"), abandoned):
        os.utime(path, (0, 0))
    os.utime(abandoned / "partial.json", (0, 0))

    dry_run = collect_artifact_garbage(
        store,
        minimum_age_seconds=0,
        clock=lambda: 100,
    )

    assert dry_run["mode"] == "dry_run"
    assert dry_run["blobs"]["total_count"] == 3  # type: ignore[index]
    assert dry_run["blobs"]["reachable_count"] == 2  # type: ignore[index]
    assert dry_run["blobs"]["eligible_count"] == 1  # type: ignore[index]
    assert dry_run["temporary"]["eligible_count"] == 1  # type: ignore[index]
    assert (store.blob_root / garbage.content_id.removeprefix("sha256:")).exists()
    assert abandoned.exists()

    deleted = collect_artifact_garbage(
        store,
        delete=True,
        minimum_age_seconds=0,
        clock=lambda: 100,
    )

    assert deleted["blobs"]["deleted_count"] == 1  # type: ignore[index]
    assert deleted["temporary"]["deleted_count"] == 1  # type: ignore[index]
    assert not (store.blob_root / garbage.content_id.removeprefix("sha256:")).exists()
    assert not abandoned.exists()
    assert (store.blob_root / parent.content_id.removeprefix("sha256:")).is_file()
    assert (store.blob_root / child.content_id.removeprefix("sha256:")).is_file()


def test_gc_protects_recent_unreferenced_blobs(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    garbage = _json_blob(store, {"value": "new"})
    path = store.blob_root / garbage.content_id.removeprefix("sha256:")
    os.utime(path, (95, 95))

    report = collect_artifact_garbage(
        store,
        delete=True,
        minimum_age_seconds=10,
        clock=lambda: 100,
    )

    assert report["blobs"]["protected_recent_count"] == 1  # type: ignore[index]
    assert report["blobs"]["deleted_count"] == 0  # type: ignore[index]
    assert path.is_file()


def test_gc_fails_closed_when_a_manifest_is_invalid(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    garbage = _json_blob(store, {"value": "keep when uncertain"})
    invalid = store.manifest_root / "broken.json"
    invalid.write_text("not-json", encoding="utf-8")

    with pytest.raises(IntegrityError, match="invalid manifest"):
        collect_artifact_garbage(
            store,
            delete=True,
            minimum_age_seconds=0,
        )

    assert (store.blob_root / garbage.content_id.removeprefix("sha256:")).is_file()


def test_model_call_and_trace_manifests_make_evidence_reachable(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    observer = ModelCallObserver(
        store,
        role="solver",
        run_id="run-1",
        packet_id=f"sha256:{'1' * 64}",
        work_id=f"sha256:{'2' * 64}",
        attempt_id=f"sha256:{'1' * 64}",
        experiment_revision_id=f"sha256:{'3' * 64}",
        stage="solve",
    )
    record = observer.completed(
        provider_id="openai_compatible",
        model="solver-model",
        parameters={"temperature": 0.0},
        messages=({"role": "user", "content": "solve"},),
        request={"messages": [{"role": "user", "content": "solve"}]},
        response={"choices": [{"message": {"content": "answer"}}]},
        usage={"total_tokens": 2},
        latency_seconds=0.1,
        configured_cost=0.0,
    )
    trace = _json_blob(store, {"role": "solver", "outputs": {"response": "answer"}})
    observer.record_trace(trace, (record,))
    repeated_record = observer.completed(
        provider_id="openai_compatible",
        model="solver-model",
        parameters={"temperature": 0.0},
        messages=({"role": "user", "content": "solve"},),
        request={"messages": [{"role": "user", "content": "solve"}]},
        response={"choices": [{"message": {"content": "answer"}}]},
        usage={"total_tokens": 2},
        latency_seconds=0.1,
        configured_cost=0.0,
    )
    observer.record_trace(trace, (repeated_record,))

    call_manifest = json.loads(
        (
            store.manifest_root
            / "runs"
            / "run-1"
            / "model-calls"
            / f"{record.call_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert call_manifest["packet_id"] == f"sha256:{'1' * 64}"
    assert call_manifest["request_artifact"]["content_id"] == record.request_artifact.content_id
    trace_manifests = tuple((store.manifest_root / "runs" / "run-1" / "traces").glob("*.json"))
    assert len(trace_manifests) == 2

    report = collect_artifact_garbage(
        store,
        delete=True,
        minimum_age_seconds=0,
    )

    assert report["blobs"]["total_count"] == 3  # type: ignore[index]
    assert report["blobs"]["reachable_count"] == 3  # type: ignore[index]
    assert report["blobs"]["deleted_count"] == 0  # type: ignore[index]
