# 012 — Legacy Export and Generic Reporting

Status: Superseded by task 017; retained only as historical implementation evidence
Priority: P1
Depends on: tasks 005, 009–011

## Outcome

Legacy schema-v0 snapshots and runs can now be copied byte-for-byte into the
external content-addressed data plane. Their source trees remain untouched and
readable. Generic summary/regression reports are also external artifacts and
group results by the complete identity needed to avoid invalid comparisons.

## Legacy Export

`LegacyArchiveExporter` recursively inventories regular files, rejects symlinks,
stores every byte stream by SHA-256, preserves its original logical path, and
publishes an immutable tree manifest. Re-exporting unchanged input is idempotent.
The manifest identifies `source_schema_version: 0`; export does not pretend an
old run had identities that were never captured.

The checked-in historical trees were exported to the disposable external root
`/private/tmp/agent-eval-legacy-export` during verification:

| Source | Namespace | Tree hash | Manifest content id |
|---|---|---|---|
| `sdk/` | `snapshots` | `sha256:7f601a19a43ccf82ca315a8bec635e89ff35cac1a23fd46468e9654af3f31a53` | `sha256:1dd69e33c404f6f2635ad25e94d56fbb06a16b27097573313c701f36a775e62f` |
| `runs/sdk/` | `runs` | `sha256:cbad2b4fe61189cb8181bc6f6268ca5946d30d387ac9c541fde68ae0fc2b6ec4` | `sha256:b71c5007308b5b8c143fd667df459f9b3f139732ab2db6118a7687c5ed9b4de1` |

This external path is evidence from the current machine, not a committed or
portable publication location. Another installation can reproduce the manifests
from the preserved source trees.

Example:

```bash
agent-eval legacy export \
  --source runs/sdk \
  --source-label runs/sdk \
  --namespace runs \
  --workspace-root . \
  --data-root "$MS_AGENT_EVAL_DATA_ROOT"
```

## Legacy Read Compatibility

`read_legacy_run` adapts the existing schema-v0 manifest/evaluation into the
generic report record without rewriting the source. Missing provenance is
rendered as `unresolved`, the split is `legacy-unassigned`, and reports emit a
warning. The historical `or-001` result remains score `0.25`, failed, with its
legacy evaluator identity; it is not silently rescored or relabeled.

## Generic Reporting Contract

Summary groups always include:

- suite id/version;
- target, source commit, and snapshot;
- instruction bundle and unit;
- program, engine, module, adapter, and compiled artifact or `uncompiled`;
- DSPy version and optimizer lock when applicable;
- split role;
- provider, model, and parameter hash;
- evaluator name/version.

Development and test results therefore cannot fall into the same aggregate.
Likewise, two source commits or base/compiled variants cannot be compared without
the revision/variant appearing in the report.

Regression reports require identical case ids and locked target/suite/source/
split/provider/evaluator axes. They reject mismatches instead of producing a
misleading delta. Program/compiled identity may differ because that is the
intended comparison axis.

Both report commands publish through the selected external artifact store:

```bash
agent-eval report summary \
  --records records.json \
  --report-id experiment-summary-v1 \
  --workspace-root . \
  --data-root "$MS_AGENT_EVAL_DATA_ROOT"

agent-eval report regression \
  --baseline base.json \
  --candidate compiled.json \
  --report-id base-vs-compiled-v1 \
  --workspace-root . \
  --data-root "$MS_AGENT_EVAL_DATA_ROOT"
```

## Verification

Python 3.12 tests cover byte preservation, deterministic re-export, symlink
rejection, legacy-run adaptation, unresolved-provenance warnings, split/revision/
compiled grouping, and locked-axis regression rejection. Raw results and reports
are never written under the Git workspace by these commands.
