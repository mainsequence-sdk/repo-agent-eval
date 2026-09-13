# Terminology

| Term | Definition |
| --- | --- |
| Workspace | Versioned directory containing the manifest and evaluation inputs. |
| Experiment | Named evaluation or optimization configuration inside a workspace. |
| Evaluation identity | Locked correctness contract needed to compare results. |
| Case package | One task with separate solver-visible and judge-only material. |
| Capability | Independently testable behavior owned by one selected skill. |
| Leakage group | Related cases assigned to one split as a unit. |
| Pilot | Diagnostic train execution with invalid benchmark status. |
| Baseline | Uncompiled solver evaluation defined by `mode: evaluate`. |
| Optimization | Grouped K-fold model selection within train, refitting on all train cases, then one held-out test evaluation. |
| Training | In this project, DSPy compilation; not model-weight training. |
| Candidate | Solver program/model configuration being evaluated. |
| Fitted candidate | State-only DSPy program artifact created for one candidate and fold. |
| Out-of-fold score | Score produced for a training case by a candidate fitted without that case's leakage group. |
| Selected configuration | Candidate with the highest mean out-of-fold score, using deterministic tie-breaking. |
| Stage packet | Immutable instructions, inputs, dependencies, executor/model identity, schema, and budget. |
| Stage result | Validated outputs, usage, worker/model evidence, and timestamps bound to a packet. |
| External data root | Non-Git storage for snapshots, blobs, manifests, runs, and temporary attempts. |
