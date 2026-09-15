# Workspace Schema Reference

The current manifest schema is version 15. Unknown fields, legacy schemas,
ambiguous instruction selection, unsafe paths, and invalid cross-references fail
closed.

Starting a run from this mutable manifest creates an immutable experiment
revision. The schema-v4 run manifest stores the full workspace hash as
provenance but keys reuse with the selected effective definition. It also
references a schema-v2 frozen definition so an existing run can be resumed
without adopting later workspace edits. See
[Incremental execution](../execution/incremental-execution.md).

| Path | Required content |
| --- | --- |
| `workspace.id` | Stable workspace identifier. |
| `workspace.data_root` | Optional external storage root. |
| `recording` | Boolean enablement, recorder and queue ids, projection phases, `continue` failure policy, artifact policy, content policy, and optional exit waiting. |
| `recorders.<id>` | MLflow HTTP(S) tracking URI, experiment-name template, and non-secret authentication references. |
| `recorder_queues.<id>` | Filesystem queue path, live coalescing, lease duration, and retry limit. |
| `executors.<id>` | Executor `type`, `adapter`, and adapter-specific options; endpoint executors require a non-empty `endpoints` list. |
| `executors.<id>.endpoints[]` | Unique endpoint `id`, HTTP(S) `base_url`, positive physical `max_concurrent_requests`, optional `api_key_env`, and non-empty deployment `routes`. |
| `executors.<id>.endpoints[].routes[]` | Deployment id eligible on this endpoint and exact request `model` sent to its API. |
| `deployments.<id>` | Serving executor, canonical model identity, and immutable output-affecting revision. |
| `models.<id>` | Deployment reference, optional `reasoning_effort`, role-specific parameters, and cost metadata. |
| `stages.<stage>` | Required reuse policy and budget. `author` and deterministic stages bind executors globally; experiment-driven model stages derive them from roles. |
| `evaluation.repository` | HTTPS URL and tag/full commit. |
| `evaluation.instructions.global` | Explicit global instruction files. |
| `evaluation.instructions.skills` | Exactly one directory or exact file list. |
| `evaluation.case_authoring` | Policy file and optional pinned reference sources. |
| `evaluation.cases` | Case directory, capability catalog, and readiness floors. |
| `evaluation.splits` | Split manifest plus deterministic group policy. |
| `evaluation.judge` | Typed DSPy judge, judge validation directory, and repetitions. Repetitions default to one; values above one are explicit consensus experiments. |
| `experiments.<id>.roles` | Solver and judge profile ids, plus an optimizer profile for COPRO. |
| `experiments.<id>` | `evaluate` baseline or `optimize` arm with exactly one `labeled_few_shot` or `copro` strategy. |
| `comparisons.<id>` | Explicit baseline and optimization experiment ids, primary metric, and solver/optimizer grouping dimensions. |

Author stages may use a harness because case packages require review and direct
workspace edits. Optimizer, solver, and judge executors resolve through the
profile → deployment → executor → eligible endpoint route chain. `optimize` is
called only by COPRO; labeled-few-shot fitting has no optimizer-model calls.
Fit, model selection, and aggregation can use a local process because they
coordinate already locked artifacts rather than make semantic decisions.
Recording settings have their own operational configuration hash and do not
participate in experiment revision, stage work, result, or cache identities.
`benchmark_revision_id`, `comparison_group_id`, and `arm_id` independently
identify the protocol, fixed-judge comparison population, and model treatment.

See [Workspace configuration](../experiments/workspace-configuration.md) for an
annotated skeleton, [Experiment recording](../execution/experiment-recording.md),
and the example manifests under `experiments/` for complete instances.
