# Data Splits and Validation

`cases/splits.yaml` assigns leakage groups—not individual prompts—to one of
three permanent splits:

| Split | Used for fitting? | Used for selection? | Purpose |
| --- | --- | --- | --- |
| Train | Yes | Yes, through a derived fixed validation holdout | Supply optimization training and selection evidence. |
| Test | No | No | Evaluate the frozen selected program once. |
| Challenge | No | No | Optional out-of-distribution or stress evaluation. |

There is no permanent validation or development split. Every optimization
experiment derives one deterministic, group-safe holdout inside permanent
train:

```yaml
validation:
  strategy: holdout
  fraction: 0.2
  seed: 0
```

All cases in one leakage group remain together. The assignment is stratified by
skill when a skill has enough groups to appear on both sides. COPRO preflight
requires every selected skill to have optimization-training and validation
evidence; otherwise the run is rejected before model calls.

`labeled_few_shot` fits every configured `k` plus `k=0` once on optimization
training and selects the highest mean validation score. COPRO consumes the
complete optimization-training set in one stateful search. The winner and
scored history from one configured round initialize the next, while every
candidate is compared on the same fixed validation cases.

After selection, the coordinator materializes the winner once. Test and
challenge evidence never becomes optimizer, fitting, validation-selection, or
fine-tuning input. COPRO may consume feedback only from the fixed validation
set: the next proposal receives complete prior candidate text plus the judge's
per-case overall score, criterion scores, hard failures, and feedback. It never
receives the expected response, artifacts, or raw rubric that produced those
diagnostics.

Complete history is the default. If exact admission or the backend proves it
cannot fit any eligible model context, a new work node retains the complete
winner and all validation diagnostics while representing losing text as
deterministic structural deltas. It never falls back to scalar scores alone.

See [ADR-011](../architecture/011-stateful-optimization-and-semantic-reuse.md)
for the state and reuse invariants.
