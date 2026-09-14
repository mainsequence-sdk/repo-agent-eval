# Resume and Recovery

Runs are append-only state machines. Resume inspects persisted attempts and
continues from the first incomplete dependency. Starting a separate run also
reuses eligible validated work from earlier runs when its stable `work_id`
matches.

## Inspect and resume

```bash
uv run ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
uv run ms-agent-eval run resume <run-id> --workspace workspace.yaml
```

Use the same `--pilot` flag when resuming or operating a pilot. The stored
execution mode, benchmark-validity flag, selected splits, and judge repetitions
must agree.

## External stage recovery

Artifact-exchange workers follow this lifecycle:

1. `stage prepare` creates the immutable packet when its dependencies exist.
2. `stage claim` creates a bounded worker lease.
3. The worker executes only the packet's declared inputs and instructions.
4. `stage submit` validates the structured result against the packet and output
   schema.
5. The coordinator persists validated outputs and advances dependent stages.

If an attempt fails or a lease expires, `stage retry` creates a new attempt.
The old packet, result, error, and transitions remain evidence; they are never
overwritten. Retrying with changed source, cases, models, or configuration
requires a new experiment lock and run rather than mutation of the old one.

For endpoint-backed automatic stages, `run resume` creates that new immutable
attempt automatically when the latest failure is classified as a connection,
provider-server, or rate-limit error. Persisted siblings are reused. Other
failure kinds remain terminal until the operator uses an appropriate explicit
recovery action; resume does not repeatedly replay invalid output or
authentication failures.

The schema-v4 run manifest records both its full originating workspace hash and
selected `experiment_revision_id`, plus direct references to its immutable
experiment lock, bundle, and definition. An unrelated workspace edit does not
change the revision. A relevant edit creates a new current revision, but it
does not prevent the old run from resuming: the coordinator rehydrates the old
definition from content-addressed storage and verifies that it reproduces the
recorded revision before preparing or executing another stage.

The two operator intents are therefore explicit:

```bash
# Continue exactly the frozen old revision, even if workspace.yaml changed.
uv run ms-agent-eval run resume <run-id> --workspace workspace.yaml

# Compile the edited workspace as a new revision and reuse compatible old work.
uv run ms-agent-eval run baseline --workspace workspace.yaml
```

Rehydration verifies the selected workspace configuration, case and judge-validation
files, capability catalog, split file, target snapshot, every authoring-source
snapshot, and the reconstructed experiment lock. Missing or corrupt immutable
inputs fail closed. The current workspace is never used as an approximate
substitute for an old run.

The public workspace loader accepts only the current schema. Frozen run
definitions are different: they are immutable framework artifacts and must
remain resumable after a schema release. Resume therefore uses explicit,
versioned internal readers for supported historical workspace and experiment-lock
identities. The reader upgrades only the in-memory runtime routing required by
the current coordinator while preserving the recorded revision, model hashes,
executor hash, cases, and configuration. It never rewrites the frozen artifact
and does not make a legacy `workspace.yaml` valid as user input. Unsupported or
semantically inconsistent historical definitions still fail closed.

Failures and stderr are bounded and secrets are redacted before persistence.
For automatic Codex stages, the disposable attempt directory is removed after
output extraction while content-addressed evidence remains in external storage.

See [Portable staged execution](../architecture/001-portable-staged-execution.md)
for packet and idempotency details.
Cross-run eligibility and invalidation are documented in
[Incremental execution](incremental-execution.md).
