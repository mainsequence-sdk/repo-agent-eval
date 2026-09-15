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
| Optimization | Selection on a fixed group-safe validation holdout derived from train, one final program materialization, then held-out test evaluation. |
| Training | In this project, DSPy compilation; not model-weight training. |
| Candidate | Solver program/model configuration being evaluated. |
| Fitted candidate | State-only DSPy program artifact created for one candidate and optimization-training corpus. |
| Validation score | Score for a candidate on the fixed validation holdout derived from permanent train. |
| COPRO round | One stateful proposal/evaluation/selection step whose winner and score history carry into the next round. |
| Selected configuration | Candidate with the highest mean validation score, using deterministic tie-breaking. |
| Work identity | Hash of semantic model work: exact model-visible content, relevant artifacts, model/deployment/parameters, adapter, limits, and output contract. |
| Attempt identity | Run-specific envelope hash containing orchestration placement, dependencies, inputs, budget, and retry number. |
| Stage packet | Immutable instructions, inputs, dependencies, executor/model identity, schema, and budget. |
| Stage result | Validated outputs, usage, worker/model evidence, and timestamps bound to a packet. |
| External data root | Non-Git storage for snapshots, blobs, manifests, runs, and temporary attempts. |
