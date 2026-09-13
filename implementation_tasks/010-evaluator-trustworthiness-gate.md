# 010 — Evaluator Trustworthiness, Calibration, and DSPy Metric Projection

Status: Implemented on 2026-08-19
Priority: P0
Depends on: tasks 001–009
Unblocks: governed optimization and authoritative reporting

## Outcome

Evaluation now fails closed at a neutral framework boundary. A case owns its
evaluator selection, exact-name registration is mandatory, and the provider is
not called when an automatic evaluator is unavailable or the case is explicitly
manual/not-evaluable. An unscored response can be requested explicitly, but its
evaluation record has null `score` and `passed` fields.

MainSequence rules live under
`experiments/mainsequence-sdk/evaluators/mainsequence/plugin.py`. The neutral
library contains only the case/rubric contract, registry, calibration record,
trusted workspace-plugin loader, runner integration, and stable result schema.
The evaluated library remains data/configuration, never a framework dependency
or core branch.

## Implemented Boundaries

### Neutral core

`ms_agent_eval.core.evaluation` provides:

- typed `active`, `manual_review_required`, and `not_evaluable` case metadata;
- strict method/status pairing and rubric validation;
- exact evaluator registration with duplicate and unknown-name rejection;
- a minimum calibration gate of one positive, two negative, and one adversarial
  fixture;
- rubric-owned weighting and exact criterion-id/range validation;
- explicit `evaluated`, `manual_review_required`, `not_evaluable`, and
  `evaluator_error` records;
- evaluator-owned immutable name/method/version identity.

`ExperimentRunner.execute_evaluated` performs evaluator preflight before job
state transition or model execution. It stores the result through the external
artifact store and registers the artifact in SQLite. It does not write raw
results into this repository.

### MainSequence experiment evaluator

The MainSequence workspace owns the namespaced evaluator
`mainsequence.orchestration-recurring-artifact-v1`. Its rules implement the six
criterion ids authored in `or-001-recurring-artifact-job/rubric.yaml`, including
explicit contradiction checks rather than positive-keyword counting alone.

Calibration uses one ideal fixture, four known-bad fixtures, and two adversarial
fixtures. The historical incorrect Ollama response remains a failing regression
fixture. Startup registration calibrates first and refuses to publish the
evaluator when any fixture expectation changes.

The generic CLI loads the explicitly configured evaluator and validates the
complete case bank against its calibrated exact registry:

```bash
ms-agent-eval evaluator validate mainsequence-rules-v1 \
  --suite mainsequence-agent-skills-v2 \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

### DSPy projection

`DspyMetricAdapter` calls `EvaluationService`; it contains no scoring rules. It
projects either the numeric score or an approved score/feedback pair and stores
the full authoritative evaluation as a content-addressed external artifact.
Unknown cases, missing typed responses, unscored statuses, and evaluator errors
raise metric errors. They are never silently converted to zero.

## Case-Bank Migration

The mechanical migration updated the original v1/v2 trees and their namespaced
copies without changing prompt, expected-response, rubric, or artifact content.
For v2 (74 cases):

- 1 case is `active`:
  `mainsequence.orchestration-recurring-artifact-v1`;
- 5 DataNode cases are `manual_review_required` through
  `mainsequence.data-node-storage-first-manual-v1`;
- 68 cases are `not_evaluable` through
  `mainsequence.pending-evaluator-v1`.

All v1 cases are explicitly `not_evaluable`. This intentionally reduces claimed
coverage: unsupported heuristics can no longer emit authoritative numbers.

The exact SDK instruction source used to author and calibrate the active case is:

```text
target: mainsequence-sdk
requested ref: v4.4.5
resolved commit: 3b5a20a344cec0c960351dc3c601d32a66a8b46e
bundle: mainsequence-agent-skills
unit id: platform_operations/orchestration_and_releases
source path: mainsequence/agent_scaffold/skills/platform_operations/orchestration_and_releases/SKILL.md
snapshot path: agent_scaffold/skills/platform_operations/orchestration_and_releases/SKILL.md
```

No fallback search of `.agents/skills`, `.agent/skills`, or another skill root is
used. Alternative roots are supported only when a target configuration names
them exactly, as proven by task 004's synthetic fixtures.

## Verification

The Python 3.12 suite covers metadata validation, calibration, adversarial and
historical negatives, exact registration, fail-closed preflight before a provider
call, explicit unscored execution, external result persistence, criterion
contract failures, and the one-source DSPy metric projection.

At implementation time:

```text
ruff: all checks passed
pytest: 67 passed, 1 optional Docker test skipped
python: CPython 3.12.8
```

## Known Coverage Gap

The five DataNode cases and 68 remaining v2 cases cannot be used as optimization
metrics or authoritative benchmark scores yet. Each requires its own calibrated,
rubric-grounded evaluator (or a governed human-review workflow). Task 011 must
reject a production optimization over any split whose required cases are in this
state; it must not relax this trust gate.
