from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProgressEvent:
    """Structured, presentation-neutral execution progress."""

    kind: str
    run_id: str
    experiment_id: str
    stage: str | None = None
    packet_id: str | None = None
    work_id: str | None = None
    attempt_id: str | None = None
    experiment_revision_id: str | None = None
    case_id: str | None = None
    case_title: str | None = None
    skill: str | None = None
    split: str | None = None
    repetition: int | None = None
    position: int | None = None
    total: int | None = None
    model: str | None = None
    executor: str | None = None
    duration_seconds: float | None = None
    details: Mapping[str, object] = field(default_factory=dict)


ProgressSink = Callable[[ProgressEvent], None]


__all__ = ["ProgressEvent", "ProgressSink"]
