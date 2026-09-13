# Pilot Versus Scored Runs

A pilot is an explicitly incomplete diagnostic run for checking judge
validation and stage wiring while the case bank is still being authored. It is
not a small benchmark.

| Property | Pilot | Scored benchmark |
| --- | --- | --- |
| Command flag | `--pilot` | No pilot flag |
| Coverage | May be incomplete | Must satisfy every readiness requirement |
| Cases | Training split only | Test split after selection or the configured evaluation path |
| Optimization | Rejected | Allowed for `mode: optimize` |
| Judge repetitions | One diagnostic vote | Workspace-configured repetitions |
| `benchmark_valid` | `false` | `true` |
| Benchmark mean | Omitted | Emitted |

Pilot results never waive source, provenance, coverage, split, model, or
judge-validation requirements. Finish authoring, run `validate`, and start a new
non-pilot run for benchmark metrics.
