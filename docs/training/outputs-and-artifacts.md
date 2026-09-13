# Training Outputs and Artifacts

Optimization produces an immutable DSPy candidate and the evidence required to
reconstruct how it was created and evaluated.

| Artifact or field | What it proves |
| --- | --- |
| State-only DSPy JSON | Safe serialized demonstrations and program state; pickle and unsafe LM state are disabled. |
| `compiled_program_hash` | Identity of the exact compiled solver candidate. |
| `train_case_hash` | Identity of the train examples used for compilation. |
| `validation_assignment_hash` | Identity of the validation cases associated with the run. |
| `test_assignment_hash` | Identity of the held-out cases released for final evaluation. |
| Experiment lock | Source, policy, catalog, cases, splits, models, programs, judge, and calibration identities. |
| Stage ledger | Packet, attempt, dependency, status, result, and usage evidence. |
| Model-call records | Rendered request, response, role, provider/model, parameters, token usage, latency, and configured cost. |
| Run summary | Held-out per-case scores, pass/fail, mean score, pass rate, and role usage. |

These artifacts live under `workspace.data_root` or
`~/ms_agent_eval/<workspace-id>/operations`, not in the experiment Git
directory. Every terminal run also creates deterministic human-readable output
under `summary/runs/<run-id>/`: `summary.md`, `results.csv`, `results.json`, and
one Markdown report per judged case repetition.

The framework does not emit model weights. A later fine-tuning export, if built,
may use only reviewed train/validation material with full provenance; it must
never include test/challenge tasks, expected responses, solver responses, or
judge feedback.

See [Storage and provenance](../architecture/storage-and-provenance.md) for the
external directory layout.
