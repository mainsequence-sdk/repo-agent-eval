from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

from ms_agent_eval.core.coordinator import StageCoordinator
from ms_agent_eval.core.endpoint_pool import EndpointPool
from ms_agent_eval.core.workspace import ExecutorConfiguration


def _executor(*, first: int = 8, second: int = 4) -> ExecutorConfiguration:
    return ExecutorConfiguration.from_mapping(
        "lan-inference",
        {
            "type": "endpoint",
            "adapter": "openai_compatible",
            "endpoints": [
                {
                    "id": "lan-10",
                    "base_url": "http://192.168.1.10:11434/v1",
                    "max_concurrent_requests": first,
                },
                {
                    "id": "lan-17",
                    "base_url": "http://192.168.1.17:11434/v1",
                    "max_concurrent_requests": second,
                },
            ],
        },
    )


def test_pool_routes_up_to_the_sum_of_endpoint_capacities(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pool = EndpointPool(_executor(), tmp_path / "locks")
    release = Event()
    all_acquired = Event()
    state_lock = Lock()
    active = {"lan-10": 0, "lan-17": 0}
    maximum = {"lan-10": 0, "lan-17": 0, "total": 0}

    def work() -> str:
        with pool.lease() as lease:
            with state_lock:
                active[lease.endpoint.id] += 1
                maximum[lease.endpoint.id] = max(
                    maximum[lease.endpoint.id], active[lease.endpoint.id]
                )
                maximum["total"] = max(maximum["total"], sum(active.values()))
                if sum(active.values()) == 12:
                    all_acquired.set()
            assert release.wait(timeout=2)
            with state_lock:
                active[lease.endpoint.id] -= 1
            return lease.endpoint.id

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(work) for _ in range(12)]
        try:
            assert all_acquired.wait(timeout=2)
        finally:
            release.set()
        routed = [future.result(timeout=2) for future in futures]

    assert routed.count("lan-10") == 8
    assert routed.count("lan-17") == 4
    assert maximum == {"lan-10": 8, "lan-17": 4, "total": 12}


def test_independent_pool_instances_share_filesystem_slots(tmp_path) -> None:  # type: ignore[no-untyped-def]
    executor = ExecutorConfiguration.from_mapping(
        "one-slot",
        {
            "type": "endpoint",
            "adapter": "openai_compatible",
            "endpoints": [
                {
                    "id": "only",
                    "base_url": "http://localhost:8000/v1",
                    "max_concurrent_requests": 1,
                }
            ],
        },
    )
    first = EndpointPool(executor, tmp_path / "locks")
    second = EndpointPool(executor, tmp_path / "locks")
    attempted = Event()
    acquired = Event()

    def acquire_second() -> None:
        attempted.set()
        with second.lease():
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with first.lease():
            future = workers.submit(acquire_second)
            assert attempted.wait(timeout=1)
            assert not acquired.wait(timeout=0.1)
        future.result(timeout=1)

    assert acquired.is_set()


def test_pool_never_leases_an_excluded_endpoint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pool = EndpointPool(_executor(first=3, second=2), tmp_path / "locks")

    selected = []
    for _ in range(6):
        with pool.lease(excluded_endpoint_ids=frozenset({"lan-10"})) as lease:
            selected.append(lease.endpoint.id)

    assert selected == ["lan-17"] * 6


def test_coordinator_executes_only_a_bounded_ready_window() -> None:
    configured = _executor(first=3, second=2)
    packets = tuple(object() for _ in range(11))
    state_lock = Lock()
    active = 0
    maximum_active = 0
    sessions: set[object] = set()

    class Coordinator:
        def _executor(self, packet):  # type: ignore[no-untyped-def]
            del packet
            return configured

        def _ensure_executed(self, packet, lock, session, *, position, total):  # type: ignore[no-untyped-def]
            del packet, lock, position
            assert total == len(packets)
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                sessions.add(session)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return True

    coordinator = Coordinator()
    pending = StageCoordinator._execute_all(  # type: ignore[arg-type]
        coordinator,
        packets,
        object(),
        SimpleNamespace(environment={}),
    )

    assert pending is None
    assert maximum_active == 5
    assert len(sessions) == len(packets)


def test_coordinator_stops_scheduling_new_packets_after_first_failure() -> None:
    configured = _executor(first=2, second=1)
    packets = tuple(object() for _ in range(20))
    started: list[int] = []
    failure_returning = Event()
    state_lock = Lock()

    class Coordinator:
        def _executor(self, packet):  # type: ignore[no-untyped-def]
            del packet
            return configured

        def _ensure_executed(self, packet, lock, session, *, position, total):  # type: ignore[no-untyped-def]
            del packet, lock, session, total
            with state_lock:
                started.append(position)
            if position == 1:
                failure_returning.set()
                return False
            assert failure_returning.wait(timeout=1)
            time.sleep(0.05)
            return True

    coordinator = Coordinator()
    pending = StageCoordinator._execute_all(  # type: ignore[arg-type]
        coordinator,
        packets,
        object(),
        SimpleNamespace(environment={}),
    )

    assert pending is packets[0]
    assert sorted(started) == [1, 2, 3]
