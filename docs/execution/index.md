# Execution

Every stage uses the same immutable packet/result lifecycle. An executor decides
where the stage runs; it does not change what the packet means or which evidence
must be recorded.

## Portable stage lifecycle

```mermaid
stateDiagram-v2
    [*] --> Prepared
    Prepared --> Claimed
    Claimed --> Submitted
    Submitted --> Validated
    Validated --> Persisted
    Claimed --> Failed
    Failed --> Prepared: retry creates a new attempt
    Persisted --> [*]
```

Packets bind the run, stage, dependencies, executor, model, instructions,
inputs, output schema, and budget. Packet schema v2 separates a stable
`work_id` from its run-specific `attempt_id`. Results bind both identities, worker,
observed model, output artifacts, usage, timestamps, and failure evidence.

## Executor choices

| Executor | Use it for | Model authentication | Isolation |
| --- | --- | --- | --- |
| OpenAI-compatible endpoint | Solver, judge, or other model-driven DSPy stages served over HTTP | Optional key named by `api_key_env` | Remote service boundary |
| Codex CLI harness | Subscription-backed model stages in disposable containers | Dedicated ChatGPT subscription store | Digest-pinned container plus restricted control-plane network |
| Artifact-exchange harness | A human or external agent that claims packets and submits structured results | Owned by the external worker | Defined by the external worker |
| Local process | Deterministic coordination such as optimization publication, final-evaluation release, and aggregation | None | Framework process |

The executor, model, and stage binding are separate configuration objects. This
allows two roles to share an endpoint while keeping distinct model identities,
parameters, budgets, and evidence.

## Automatic and external operation

Run the complete coordinator:

```bash
uv run ms-agent-eval run baseline --workspace workspace.yaml
```

Pause after a stage and inspect the ledger:

```bash
uv run ms-agent-eval run baseline --through solve --workspace workspace.yaml
uv run ms-agent-eval stage list --run <run-id> --workspace workspace.yaml
uv run ms-agent-eval run resume <run-id> --workspace workspace.yaml
```

External workers use `stage prepare`, `stage claim`, and `stage submit`. A retry
never mutates the failed attempt; it creates a new immutable attempt attached to
the same logical stage.

Choose [OpenAI-compatible API](openai-compatible-api.md) for HTTP model servers
or [Codex CLI](codex-cli.md) for the subscription-backed isolated harness. The
recovery workflow is documented in [Resume and recovery](resume-and-recovery.md).
The security and protocol rationale lives in
[ADR-001](../architecture/001-portable-staged-execution.md) and
[ADR-002](../architecture/002-isolated-codex-cli-execution.md).

For cross-run cache eligibility, invalidation, model deployment revisions,
replication, storage, and report accounting, read
[Incremental execution and reuse](incremental-execution.md).
