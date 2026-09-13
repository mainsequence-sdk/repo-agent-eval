# Pilot Versus Scored Runs

A pilot is an explicitly incomplete diagnostic run. It exists to test
calibration and stage wiring while the case bank is still being authored; it is
not a small benchmark.

| Property | Pilot | Scored benchmark |
| --- | --- | --- |
| Command flag | `--pilot` | No pilot flag |
| Coverage | May be incomplete | Must satisfy every readiness floor |
| Available splits | Train and validation | Experiment-defined benchmark splits |
| Optimization | Rejected | Allowed for `mode: optimize` |
| Judge repetitions | Exactly one diagnostic vote | Workspace-configured repetitions |
| `benchmark_valid` | `false` | `true` |
| Mean score and pass rate | Omitted | Emitted |
| Per-case diagnostics | Retained | Retained as scored evidence |

Run a pilot:

```bash
uv run ms-agent-eval run baseline --pilot --workspace workspace.yaml
```

The resulting run manifest records `execution_mode: pilot`, selected
train/validation splits, one judge repetition, and `benchmark_valid: false`.
Resume and stage operations must retain `--pilot`; the coordinator rejects any
attempt to reinterpret the run as a benchmark or enter optimization.

A passing pilot does not waive source, provenance, coverage, split, model, or
calibration requirements. Finish authoring, run `validate`, and start a new
non-pilot run for benchmark metrics.
