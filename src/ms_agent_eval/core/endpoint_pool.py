from __future__ import annotations

import fcntl
import queue
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Iterator

from .workspace import EndpointConfiguration, ExecutorConfiguration


@dataclass(frozen=True)
class EndpointLease:
    endpoint: EndpointConfiguration
    pool_hash: str
    queue_wait_seconds: float
    endpoint_active_at_start: int
    pool_active_at_start: int

    def provenance(self) -> dict[str, object]:
        return {
            "endpoint_pool": {
                "pool_hash": self.pool_hash,
                "endpoint_id": self.endpoint.id,
                "endpoint_configuration_hash": self.endpoint.configuration_hash,
                "max_concurrent_requests": self.endpoint.max_concurrent_requests,
                "queue_wait_seconds": self.queue_wait_seconds,
                "endpoint_active_at_start": self.endpoint_active_at_start,
                "pool_active_at_start": self.pool_active_at_start,
            }
        }


class EndpointPool:
    """Capacity-aware endpoint permits shared by threads and local processes."""

    def __init__(self, executor: ExecutorConfiguration, lock_root: Path) -> None:
        if not executor.endpoints:
            raise ValueError("an endpoint pool requires at least one endpoint")
        self.executor = executor
        self.pool_hash = executor.pool_hash.removeprefix("sha256:")
        self.lock_root = lock_root / self.pool_hash
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self._tokens: queue.Queue[tuple[EndpointConfiguration, int]] = queue.Queue()
        for endpoint in executor.endpoints:
            endpoint_root = self.lock_root / endpoint.id
            endpoint_root.mkdir(parents=True, exist_ok=True)
        for slot in range(max(endpoint.max_concurrent_requests for endpoint in executor.endpoints)):
            for endpoint in executor.endpoints:
                if slot < endpoint.max_concurrent_requests:
                    self._tokens.put((endpoint, slot))
        self._metrics_lock = Lock()
        self._active_by_endpoint = {endpoint.id: 0 for endpoint in executor.endpoints}
        self._pool_active = 0

    @property
    def capacity(self) -> int:
        return self.executor.total_concurrency

    @contextmanager
    def lease(self) -> Iterator[EndpointLease]:
        started = time.monotonic()
        endpoint: EndpointConfiguration | None = None
        slot = -1
        handle = None
        while handle is None:
            candidate, candidate_slot = self._tokens.get()
            path = self.lock_root / candidate.id / f"{candidate_slot}.lock"
            candidate_handle = path.open("a+b")
            try:
                fcntl.flock(candidate_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate_handle.close()
                self._tokens.put((candidate, candidate_slot))
                time.sleep(0.01)
                continue
            endpoint = candidate
            slot = candidate_slot
            handle = candidate_handle

        assert endpoint is not None and slot >= 0
        with self._metrics_lock:
            self._active_by_endpoint[endpoint.id] += 1
            self._pool_active += 1
            endpoint_active = self._active_by_endpoint[endpoint.id]
            pool_active = self._pool_active
        lease = EndpointLease(
            endpoint=endpoint,
            pool_hash=f"sha256:{self.pool_hash}",
            queue_wait_seconds=time.monotonic() - started,
            endpoint_active_at_start=endpoint_active,
            pool_active_at_start=pool_active,
        )
        try:
            yield lease
        finally:
            with self._metrics_lock:
                self._active_by_endpoint[endpoint.id] -= 1
                self._pool_active -= 1
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._tokens.put((endpoint, slot))


def endpoint_provenance_routes(
    provenance: Mapping[str, object],
    packet_usage: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Normalize single-lease and internally fanned-out packet provenance."""

    raw_routes = provenance.get("routes")
    if isinstance(raw_routes, Sequence) and not isinstance(
        raw_routes, (str, bytes, bytearray)
    ):
        return tuple(dict(route) for route in raw_routes if isinstance(route, Mapping))
    if not isinstance(provenance.get("endpoint_id"), str):
        return ()
    route = dict(provenance)
    for key in (
        "model_calls",
        "completed_calls",
        "failed_calls",
        "tokens",
        "configured_cost",
        "latency_seconds",
    ):
        route[key] = packet_usage.get(key, 0)
    route["peak_observed_endpoint_concurrency"] = provenance.get(
        "endpoint_active_at_start", 0
    )
    route["peak_observed_pool_concurrency"] = provenance.get(
        "pool_active_at_start", 0
    )
    return (route,)


__all__ = ["EndpointLease", "EndpointPool", "endpoint_provenance_routes"]
