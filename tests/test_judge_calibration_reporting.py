from __future__ import annotations

from pathlib import Path

from ms_agent_eval.core.lifecycle import RunLifecycleLedger, RunLifecycleState
from ms_agent_eval.core.recorder import TableArtifact
from ms_agent_eval.core.recording_projection import RecorderProjector, _table_index_html
from ms_agent_eval.core.stages import (
    StageBudget,
    StageExecutorIdentity,
    StageKind,
    StageLedger,
    StageModelIdentity,
    StagePacket,
    StageResult,
)
from ms_agent_eval.core.storage import FilesystemArtifactStore
from ms_agent_eval.core.workspace import WorkspaceConfiguration

from .helpers import workspace_payload


def test_judge_calibration_table_explains_scope_repetitions_and_scores() -> None:
    table = TableArtifact(
        "table-id",
        "judge-calibration",
        (
            {
                "fixture_id": "fixture-strong",
                "case_id": "case-one",
                "skill": "data_publishing/meta_tables",
                "quality_label": "strong",
                "rubric_hash": "sha256:rubric",
                "repetitions": 3,
                "aggregate_score": 1.0,
            },
            {
                "fixture_id": "fixture-partial",
                "case_id": "case-one",
                "skill": "data_publishing/meta_tables",
                "quality_label": "partial",
                "rubric_hash": "sha256:rubric",
                "repetitions": 3,
                "aggregate_score": 0.425,
            },
        ),
    )

    page = _table_index_html((table,)).decode("utf-8")

    assert "How to read judge calibration" in page
    assert "median repetition score" in page
    assert "minimum(strong) &gt; maximum(partial)" not in page
    assert "minimum(strong) > maximum(partial)" in page
    assert "Proper multi-skill calibration" in page
    assert '"quality_label":"strong"' in page
    assert '"aggregate_score":1.0' in page


def test_calibration_rows_use_frozen_reporting_metadata(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "external")
    ledger = StageLedger(store)
    lock = store.put_manifest("fixtures/lock", {"kind": "lock"})
    bundle = store.put_manifest("fixtures/bundle", {"kind": "bundle"})
    definition = store.put_manifest(
        "fixtures/definition",
        {
            "cases": [
                {
                    "id": "case-one",
                    "title": "Case one",
                    "skill": "data_publishing/meta_tables",
                    "rubric": {
                        "criteria": [
                            {
                                "id": "correctness",
                                "weight": 1.0,
                                "description": "Correct",
                            }
                        ],
                        "hard_failures": [],
                    },
                }
            ],
            "judge_validation": {
                "fixtures": [
                    {
                        "id": "fixture-strong",
                        "case_id": "case-one",
                        "label": "strong",
                    }
                ]
            },
        },
    )
    ledger.create_run(
        "run-1",
        {
            "schema_version": 4,
            "run_id": "run-1",
            "workspace_id": "workspace",
            "workspace_hash": f"sha256:{'1' * 64}",
            "experiment_id": "baseline",
            "experiment_revision_id": f"sha256:{'2' * 64}",
            "execution_mode": "benchmark",
            "benchmark_valid": True,
            "coverage_status": "complete",
            "judge_repetitions": 3,
            "experiment_lock_artifact": lock.__dict__,
            "experiment_bundle_artifact": bundle.__dict__,
            "experiment_definition_artifact": definition.__dict__,
            "created_at": "2026-09-14T10:00:00+00:00",
        },
    )
    packet = StagePacket.create(
        run_id="run-1",
        experiment_id="baseline",
        experiment_revision_id=f"sha256:{'2' * 64}",
        stage=StageKind.VALIDATE_JUDGE,
        attempt=1,
        dependencies=(),
        inputs=(),
        instructions=ledger.put_json("instructions", {"task": "calibrate"}),
        output_schema="judge-validation-v1",
        executor=StageExecutorIdentity(
            "remote-models", "openai_compatible", f"sha256:{'3' * 64}"
        ),
        model=StageModelIdentity("judge", f"sha256:{'4' * 64}"),
        budget=StageBudget(3, 1000, 60),
        work_spec={"repetitions": 3},
    )
    ledger.prepare(packet)
    claim = ledger.claim(packet.packet_id, executor_id="remote-models", worker_id="worker")
    result = StageResult.create(
        packet=packet,
        status="completed",
        executor_version="1",
        worker_id="worker",
        outputs=(
            ledger.put_json(
                "result",
                {
                    "fixture_scores": {"fixture-strong": 1.0},
                    "ordering_consistent": True,
                },
            ),
        ),
        usage={"model_calls": 3},
        started_at="2026-09-14T10:00:01+00:00",
        completed_at="2026-09-14T10:00:02+00:00",
        observed_model="judge-model",
    )
    ledger.submit(result, lease_id=claim.lease_id)
    ledger.validate(packet.packet_id, lambda _packet, _result: None)
    ledger.persist(packet.packet_id)
    lifecycle = RunLifecycleLedger(store)
    lifecycle.append("run-1", RunLifecycleState.CREATED, reason="run_created")
    lifecycle.append("run-1", RunLifecycleState.COMPLETED, reason="run_completed")

    configuration = WorkspaceConfiguration.from_mapping(workspace_payload())
    plan = RecorderProjector(
        store,
        recorder=configuration.recorders[configuration.recording.recorder],
        recording=configuration.recording,
    ).build("run-1")
    row = next(table for table in plan.tables if table.name == "judge-calibration").rows[0]

    assert row["case_id"] == "case-one"
    assert row["case_title"] == "Case one"
    assert row["skill"] == "data_publishing/meta_tables"
    assert row["quality_label"] == "strong"
    assert row["rubric_criteria"] == ("correctness",)
    assert str(row["rubric_hash"]).startswith("sha256:")
    assert row["repetitions"] == 3
    assert row["aggregate_score"] == 1.0
