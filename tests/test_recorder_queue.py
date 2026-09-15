from __future__ import annotations

import json
import time
from pathlib import Path

from ms_agent_eval.core.recorder_queue import (
    FilesystemRecorderQueue,
    ProjectionCursor,
    ProjectionIntent,
    ProjectionIntentKind,
)


def _intent(
    *,
    kind: ProjectionIntentKind = ProjectionIntentKind.LIVE_PROGRESS,
    target_sequence: int = 1,
    boundary_id: str | None = None,
) -> ProjectionIntent:
    return ProjectionIntent.create(
        recorder_id="local-mlflow",
        recorder_config_hash=f"sha256:{'a' * 64}",
        workspace_id="workspace",
        run_id="run-1",
        experiment_revision_id=f"sha256:{'b' * 64}",
        kind=kind,
        target_sequence=target_sequence,
        priority=100 if kind is ProjectionIntentKind.FINALIZATION else 10,
        boundary_id=boundary_id,
        created_at="2026-09-15T00:00:00+00:00",
    )


def test_live_projection_intents_are_pointer_only_and_coalesced(tmp_path: Path) -> None:
    queue = FilesystemRecorderQueue(tmp_path / "queue")

    first = queue.enqueue(_intent(target_sequence=10))
    second = queue.enqueue(_intent(target_sequence=20))

    assert first["status"] == "queued"
    assert second["status"] == "queued"
    pending = list(queue.pending_root.glob("*.json"))
    assert len(pending) == 1
    payload = json.loads(pending[0].read_text(encoding="utf-8"))
    assert payload["intent"]["target_sequence"] == 20
    serialized = json.dumps(payload)
    assert "RecordingPlan" not in serialized
    assert "prompt" not in serialized
    assert "response" not in serialized
    assert "artifacts" not in serialized


def test_required_boundaries_are_not_coalesced(tmp_path: Path) -> None:
    queue = FilesystemRecorderQueue(tmp_path / "queue")

    queue.enqueue(
        _intent(
            kind=ProjectionIntentKind.STAGE_BOUNDARY,
            target_sequence=10,
            boundary_id="solve",
        )
    )
    queue.enqueue(
        _intent(
            kind=ProjectionIntentKind.FINALIZATION,
            target_sequence=20,
            boundary_id="terminal",
        )
    )

    assert len(list(queue.pending_root.glob("*.json"))) == 2


def test_worker_lease_retry_acknowledgement_and_cursor(tmp_path: Path) -> None:
    queue = FilesystemRecorderQueue(
        tmp_path / "queue",
        lease_seconds=30,
        max_attempts=2,
    )
    intent = _intent(
        kind=ProjectionIntentKind.FINALIZATION,
        target_sequence=20,
        boundary_id="terminal",
    )
    queue.enqueue(intent)

    now = time.time()
    first = queue.claim(worker_id="worker-1", now=now)
    assert first is not None
    state = queue.retry(
        first,
        error_kind="connection_error",
        message="temporary",
        delay_seconds=0,
    )
    assert state == "retrying"

    second = queue.claim(worker_id="worker-2", now=now + 1)
    assert second is not None
    cursor = ProjectionCursor.initial(intent.recorder_config_hash, intent.run_id).advance(intent)
    queue.write_cursor(cursor)
    queue.acknowledge(second, {"status": "projected"})

    status = queue.status(
        recorder_config_hash=intent.recorder_config_hash,
        run_id=intent.run_id,
    )
    assert status == {
        "pending": 0,
        "leased": 0,
        "acknowledged": 1,
        "dead_letter": 0,
        "oldest_pending_at": None,
    }
    restored = queue.cursor(intent.recorder_config_hash, intent.run_id)
    assert restored.projected_through == 20
    assert restored.finalized_through == 20


def test_expired_worker_lease_is_recovered(tmp_path: Path) -> None:
    queue = FilesystemRecorderQueue(tmp_path / "queue", lease_seconds=10)
    queue.enqueue(_intent(target_sequence=3))
    abandoned = queue.claim(worker_id="worker-1", now=100.0)
    assert abandoned is not None

    recovered = queue.claim(worker_id="worker-2", now=111.0)

    assert recovered is not None
    assert recovered.intent.intent_id == abandoned.intent.intent_id
    assert recovered.attempts == 2
