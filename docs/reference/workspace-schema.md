# Workspace Schema Reference

The current manifest schema is version 9. Unknown fields, legacy schemas,
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
| `executors.<id>` | Executor `type`, `adapter`, and adapter-specific options. |
| `models.<id>` | Model `name`, required `deployment_revision`, optional `reasoning_effort`, parameters, and cost metadata. |
| `stages.<stage>` | Executor id, model id where model-driven, required reuse policy, and budget. |
| `evaluation.repository` | HTTPS URL and tag/full commit. |
| `evaluation.instructions.global` | Explicit global instruction files. |
| `evaluation.instructions.skills` | Exactly one directory or exact file list. |
| `evaluation.case_authoring` | Policy file and optional pinned reference sources. |
| `evaluation.cases` | Case directory, capability catalog, and readiness floors. |
| `evaluation.splits` | Split manifest plus deterministic group policy. |
| `evaluation.judge` | Typed DSPy judge, calibration directory, and repetitions. |
| `experiments.<id>` | `evaluate` baseline or `optimize` configuration. |

Author stages may use a harness because case packages require review and direct
workspace edits. Solver and judge stages use configured model executors.
Optimization, final-evaluation release, and aggregation can use a local process
because they coordinate already locked artifacts rather than make semantic
decisions.

See [Workspace configuration](../experiments/workspace-configuration.md) for an
annotated skeleton and the example manifests under `experiments/` for complete
instances.
