# Terminology

| Term | Definition |
| --- | --- |
| Workspace | Versioned directory containing the manifest and evaluation inputs. |
| Experiment | Named evaluation or optimization configuration inside a workspace. |
| Evaluation identity | Locked correctness contract needed to compare results. |
| Case package | One task with separate solver-visible and judge-only material. |
| Capability | Independently testable behavior owned by one selected skill. |
| Leakage group | Related cases assigned to one split as a unit. |
| Pilot | Diagnostic train/validation execution with invalid benchmark status. |
| Baseline | Uncompiled solver evaluation defined by `mode: evaluate`. |
| Optimization | DSPy compilation on train followed by validation and held-out test evaluation. |
| Training | In this project, DSPy compilation; not model-weight training. |
| Candidate | Solver program/model configuration being evaluated. |
| Compiled candidate | State-only DSPy program artifact created by optimization. |
| Publication | Persistence of an immutable compiled state artifact and its hashes. |
| Final-evaluation boundary | Recorded dependency after validation that permits test loading. |
| Stage packet | Immutable instructions, inputs, dependencies, executor/model identity, schema, and budget. |
| Stage result | Validated outputs, usage, worker/model evidence, and timestamps bound to a packet. |
| External data root | Non-Git storage for snapshots, blobs, manifests, runs, and temporary attempts. |
