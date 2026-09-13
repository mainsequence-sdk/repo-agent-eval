# Judging and Judge Validation

The configured DSPy judge assigns continuous criterion scores in `[0, 1]` and
reports named critical violations declared by the rubric. It does not emit an
overall pass/fail verdict.

Before solver work begins, the framework checks the judge against the reviewed
`judge-validation/` set. Its schema-v3 manifest contains candidate responses
labelled `strong`, `partial`, `incorrect`, `contradictory`, or `adversarial`:

```yaml
schema_version: 3
fixtures:
  - id: strong-example
    case: training-case-id
    candidate: strong.md
    label: strong
```

Labels stay coordinator-only. The judge receives the candidate, task, rubric,
expected evidence, and skill context. Validation succeeds only when every
strong fixture scores above every partial fixture and every partial fixture
scores above every negative fixture. This is an ordinal consistency check; it
defines no numeric score ranges or case cutoff.

Judge-validation fixtures may reference training cases only. Test and challenge
cases remain untouched. The framework validates criterion identifiers, score
bounds, weighted arithmetic, median aggregation across repeated votes, and
majority aggregation of declared critical violations. Semantic assessment
remains the judge model's responsibility.
