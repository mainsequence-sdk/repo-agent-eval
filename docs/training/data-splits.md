# Training Data Splits

`cases/splits.yaml` assigns leakage groups—not individual prompts—to a split.
The workspace supplies deterministic ratios and a seed:

```yaml
evaluation:
  splits:
    file: cases/splits.yaml
    policy:
      seed: example-foundations-v1
      train: 0.5
      validation: 0.2
      test: 0.2
      challenge: 0.1
```

| Split | Optimization may read it? | Purpose |
| --- | --- | --- |
| Train | Yes | Supplies expected responses used to compile demonstrations. |
| Validation | For evaluation, not compilation | Scores the fixed compiled candidate before final evaluation. |
| Test | No, until final-evaluation boundary | Produces the held-out benchmark for the published candidate. |
| Challenge | No | Reserved for separate stress and generalization analysis. |

All paraphrases or variants derived from the same project example, defect,
architecture slice, fixture, or likely solution share one group. This prevents
the optimizer from memorizing an answer whose near-duplicate appears in test.

Judge calibration used by a pilot or optimization run may reference train and
validation cases only. Test/challenge prompts, expected responses, candidates,
and feedback never become optimization or fine-tuning inputs.

See the [Capability coverage contract](../evaluation/capability-coverage.md) for
the distinct-leakage-group readiness floor.
