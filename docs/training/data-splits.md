# Data Splits and Cross-Validation

`cases/splits.yaml` assigns leakage groups—not individual prompts—to one of
three permanent splits:

| Split | Used for fitting? | Used for model selection? | Purpose |
| --- | --- | --- | --- |
| Train | Yes | Yes, through grouped K-fold cross-validation | Fit candidates and estimate their generalization within the available training data. |
| Test | No | No | Evaluate the selected, refitted candidate once. |
| Challenge | No | No | Optional out-of-distribution or stress evaluation. |

The workspace records deterministic train/test/challenge ratios and a split
seed. It has no permanent validation or development split.

An optimization experiment separately configures grouped K-fold
cross-validation:

```yaml
optimizer:
  strategy: labeled_few_shot
  search:
    k: [1, 2, 4]
cross_validation:
  folds: 5
  seed: 0
```

The same grouped split boundary applies when `strategy: copro`; its prompt and
skill proposal/selection loop runs separately inside each K-1 fitting corpus.
For each selected configuration, every training case receives exactly one
out-of-fold score.
All cases in the same leakage group stay in the same fold. The candidate with
the highest mean out-of-fold score is selected; ties prefer the smaller `k`,
then candidate id. The selected configuration is refit on all training cases
before the test set is loaded.

Test and challenge prompts, expected responses, solver responses, judge
feedback, and metrics never become optimizer, fitting, selection, or
fine-tuning inputs. COPRO's optimizer sees candidate wording and aggregate
numeric training scores only; the separate judge alone sees correctness
evidence.
