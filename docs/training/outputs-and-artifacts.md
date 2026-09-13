# Training Outputs and Artifacts

Optimization produces an immutable fitted DSPy program and the evidence needed
to reconstruct model selection and held-out evaluation.

| Artifact or field | What it proves |
| --- | --- |
| State-only DSPy JSON | Safe serialized demonstrations and program state; pickle and unsafe LM state are disabled. |
| `fitted_program_hash` | Identity of an exact fitted solver program. |
| Fold-assignment hash | Identity of the grouped K-fold assignment within train. |
| Candidate selection result | Mean out-of-fold score for every candidate and the deterministic argmax. |
| Training-case hash | Identity of the examples used for each fit, including the final refit. |
| Experiment lock | Source, policy, catalog, cases, splits, models, programs, judge, and judge-validation identities. |
| Stage ledger | Packet, attempt, dependency, status, result, and usage evidence. |
| Model-call records | Rendered request, response, role, provider/model, parameters, token usage, latency, and configured cost. |
| Run summary | Held-out per-case scores, critical violations, mean score, cross-validation selection, and role usage. |

These artifacts live under `workspace.data_root` or
`~/ms_agent_eval/<workspace-id>/operations`, not in the experiment Git
directory. Every terminal run also creates deterministic human-readable output
under `summary/runs/<run-id>/`: `summary.md`, `results.csv`, `results.json`, and
one Markdown report per judged case repetition.

The framework does not emit model weights. A later fine-tuning export, if built,
may use only reviewed train material with full provenance; it must
never include test/challenge tasks, expected responses, solver responses, or
judge feedback.

See [Storage and provenance](../architecture/storage-and-provenance.md) for the
external directory layout.
