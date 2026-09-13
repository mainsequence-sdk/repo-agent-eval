# Workspace Configuration

`workspace.yaml` is the only framework configuration file in an experiment
workspace. Schema version 11 separates reusable execution identities from the
evaluation contract and named experiments.

## Top-level structure

| Key | Owns |
| --- | --- |
| `schema_version` | Exact workspace schema; legacy schemas are rejected. |
| `workspace` | Workspace id and optional external `data_root`. |
| `executors` | Endpoint, harness, or local-process transport configuration. |
| `models` | Model name, immutable deployment revision, reasoning effort, parameters, and configured cost. |
| `stages` | Executor/model assignment, reuse policy, and call/token/wall-time budgets. |
| `evaluation` | Repository, instructions, authoring sources, cases, splits, and judge contract. |
| `experiments` | Named baseline and optimization configurations. |

```yaml
schema_version: 11

workspace:
  id: example-evaluation

executors: {}
models: {}
stages: {}

evaluation:
  repository: {}
  instructions: {}
  case_authoring: {}
  cases: {}
  splits: {}
  judge: {}

experiments: {}
```

## Important invariants

- Repository tags resolve to full commits before model execution.
- Every repository and imported skill selection uses exactly one of
  `skills.directory` or `skills.files`.
- API addresses and model ids are versioned here; only secret values come from
  environment variables.
- Every endpoint executor contains one or more explicitly named replicas with
  a positive `max_concurrent_requests` value.
- `reasoning_effort` is a first-class model field, not a generic parameter.
- Every model requires `deployment_revision`; change it whenever weights,
  quantization, tokenizer, chat template, or hidden serving state changes.
- Every stage requires `reuse.policy`: `exact`, `refresh`, or solver-only
  `replicate` with `samples`.
- Solver and judge remain separate stage roles even when deliberately assigned
  the same configured model.
- Every stage has a finite budget.
- An optimization experiment names its baseline with `based_on`, declares a
  candidate search space, and configures grouped K-fold cross-validation.
- Generated ids, hashes, snapshots, and locks are outputs, not authored
  configuration.

Use [Target sources](target-sources.md) for `evaluation.repository` and
authoring references, [Case authoring](case-authoring.md) for the versioned
corpus, and [Execution](../execution/index.md) for executor configuration.

## Endpoint executor pools

```yaml
executors:
  lan-inference:
    type: endpoint
    adapter: openai_compatible
    endpoints:
      - id: llama-cpp-17-18081
        base_url: http://192.168.1.17:18081/v1
        max_concurrent_requests: 4
      - id: llama-cpp-17-18080
        base_url: http://192.168.1.17:18080/v1
        max_concurrent_requests: 4
      - id: llama-server-10-11436
        base_url: http://192.168.1.10:11436/v1
        max_concurrent_requests: 8
```

The executor id names the pool, not a model or a vague location such as
"remote." Its capacity is the sum of replica capacities; the example permits
sixteen concurrent requests. Endpoint ids and normalized URLs must be unique inside
the pool. A credential reference, when required, belongs on the relevant
endpoint item as `api_key_env`.

Capacity applies to independent model requests, including requests nested inside
a single stage packet. Judge validation expands each fixture/repetition pair into
one request and schedules those votes across the pool before deterministic
aggregation.

All replicas used by a model profile must be semantically equivalent.
`models.<id>.deployment_revision` is the explicit attestation of that
equivalence and must change for any output-affecting deployment change. Pool
topology and capacities are recorded as execution provenance but do not change
the model's reusable semantic work id.

## Imported instruction bundles

An evaluated repository can assemble instructions from its own pinned source
and an independently versioned external catalog. Store the reviewed external
instruction files in the experiment and declare the import explicitly:

```yaml
evaluation:
  instructions:
    global:
      - agent_scaffold/AGENTS.md
    skills:
      directory: agent_scaffold/skills
    imports:
      - id: platform
        source: mcp
        note: >-
          Platform-owned skills captured from MCP and pinned by content hash.
        manifest: instructions/mainsequence-platform/manifest.yaml
        skills:
          directory: instructions/mainsequence-platform/skills
```

Imported skill ids are namespaced as `<import-id>/<manifest-unit-id>`. The
manifest must list exactly the selected files and record a SHA-256 content id
for each one. Validation rejects missing, extra, changed, symlinked, colliding,
or unmanifested files. Source metadata in the manifest participates in the
instruction-context and experiment revision identities.

Imports are never refreshed as a side effect of `validate`, `inspect`, or
`run`. Updating one requires an explicit authenticated capture and review. This
keeps an external mutable catalog from changing a benchmark silently.

## Model deployment and reuse

```yaml
models:
  solver:
    name: qwen3.6:27b-coding
    deployment_revision: qwen3.6-27b-coding-local-2026-09-13
    reasoning_effort: high
    parameters:
      temperature: 0.2
      max_tokens: 4096

stages:
  solve:
    executor: lan-inference
    model: solver
    reuse: {policy: exact}
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
```

The deployment revision is an operator-controlled cache boundary, not a secret
and not a marketing model alias. `exact` is the normal evaluation policy.
Temporarily select `refresh` when a new attempt is required despite identical
inputs. For independent solver samples use:

```yaml
stages:
  solve:
    executor: lan-inference
    model: solver
    reuse:
      policy: replicate
      samples: 3
    budget: {model_calls: 1, tokens: 32000, wall_seconds: 600}
```

Replicates share one semantic `work_id` but receive separate attempt identities
and are never collapsed into a cache hit. Judge consensus repetitions remain a
different setting under `evaluation.judge.repetitions`.
