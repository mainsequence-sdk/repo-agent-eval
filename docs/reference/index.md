# Reference

This section collects stable cross-cutting rules. Workflow instructions live in
the task-oriented experiment, training, evaluation, and execution sections.

## Core terms

| Term | Meaning |
| --- | --- |
| Workspace | The small versioned directory containing `workspace.yaml` and evaluation inputs. |
| Experiment | A named `evaluate` or `optimize` configuration inside the workspace. |
| Case bank | Reviewed case packages with solver-visible and judge-only surfaces. |
| Capability catalog | The semantic inventory and required primary-case depth for each selected skill. |
| Leakage group | Cases derived from the same source, defect, design, or likely solution and assigned to one split as a unit. |
| Pilot | A train-only diagnostic run with `benchmark_valid: false`. |
| Benchmark | A scored run whose readiness checks and locked identity make its aggregate metrics valid. |
| Training | DSPy program compilation from train cases; it does not modify model weights. |
| Fitted candidate | Content-addressed, state-only DSPy JSON for one candidate configuration. |
| External data root | Storage for snapshots, artifacts, manifests, results, and temporary attempts. |

Read [Framework and experiment conventions](conventions.md) for authoritative
ownership, identity, judging, dataset-governance, runtime, secret, and reporting
rules.

- [CLI reference](cli.md)
- [Workspace schema reference](workspace-schema.md)
- [Terminology](terminology.md)

Related task-oriented references:

- [Experiment requirements](../experiments/index.md)
- [Training inputs and outputs](../training/index.md)
- [Evaluation readiness](../evaluation/index.md)
- [Executor choices](../execution/index.md)
- [Repository structure](../architecture/repository-structure.md)
