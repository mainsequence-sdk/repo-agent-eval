# Case Similarity Audit

Use the offline audit during case authoring to find repeated solver inputs,
reference solutions, and highly similar pairs that deserve review. It reads
local case packages, verifies their content hashes, and compares cases within
each selected skill. It makes no model calls, fetches no source snapshots, and
does not change cases, splits, acceptance status, or evaluation readiness.

## Run an audit

```bash
ms-agent-eval cases similarity \
  --workspace experiments/metatables/workspace.yaml \
  --all-skills --view both --top-k 10
```

Use `--skill <exact-id>` instead of `--all-skills` to select a skill; repeat
`--skill` for several. Nested IDs such as `data_publishing/meta_tables` are
supported. A skill must appear in the local capability catalog. Coverage depth
and judge-validation readiness need not be complete.

`--view solver` is the default: prompt plus solver-visible text attachments.
`--view expected` compares expected responses and expected text artifacts.
`--view both` calculates them independently. IDs, groups, capability labels,
rubrics, source paths, and shared skill instructions are excluded from vectors.
Exact equality includes attachment paths and content. Identical vectors alone
do not imply identical input bytes.

The only representation is currently `--representation tfidf`, with Unicode
casefolding, word unigrams of at least two characters, sublinear term counts,
smoothed inverse document frequency, and L2 normalization. Corpus membership
affects fitted weights. No model endpoint or credential is required.

Every unique pair within each skill is compared. `--top-k` selects nearest
neighbors per case and per view for the compact CSV; it never samples the
corpus. Equal-score ties prefer case-ID order. The CSV also retains the top
twenty pairs per category and view, including cross-split and different-group
comparisons. Each unique selected pair appears once.

`--block-size` controls memory use (default 256). `--export-pairs` additionally
writes every unique pair to `pairs.csv`; this can be very large. For 5,000 cases
there are 12,497,500 pairs per skill. Standard output is a JSON result with
report paths; `--json` additionally suppresses progress on stderr.

## Outputs and storage

Outputs use the workspace's external data root. For the MetaTables workspace,
the default is `~/ms_agent_eval/metatables-evaluation`:

```text
operations/
  blobs/sha256/                                  vectors and report evidence
  manifests/case-similarity/                      immutable audit identities
  manifests/case-similarity-vectors/              corpus-specific vector caches
  manifests/case-similarity-reports/              report projections
  manifests/case-similarity-bundles/               multi-skill report indexes
summary/case-similarity/
  <bundle-id>/summary.md                          links to selected skill reports
  <bundle-id>/summary.json                        command result and paths
  <audit-id>/<report-id>/
    summary.md                                   human-readable findings
    summary.json                                 complete aggregate findings
    neighbors.csv                                compact selected-pair table
    exact-families.json                           every exact-duplicate family
    pairs.csv                                    only with --export-pairs
```

Nothing is written into case folders. Default reports retain aggregates,
bounded pair rankings, and exact families rather than a full score matrix.
The JSON includes score histograms and counts for all pairs, same-group pairs,
different-group pairs, and cross-split pairs. These categories overlap; their
counts must not be added together.

Vector caches identify the exact rendered corpus and representation settings.
Changing a case's metadata can reuse vectors while producing a new annotated
report; adding text to the corpus changes TF-IDF weights and invalidates the fit.
Changing `top-k` creates a new report projection using the same vector cache.

## Interpretation and boundaries

A high lexical score is a review signal. Shared templates can inflate scores,
and different wording can hide the same reasoning task. Cosine scores are not
case acceptance decisions, novelty certificates, or leakage verdicts. Histogram
bands are descriptive, not pass/fail thresholds. Inspect both the task and
reference solution before deciding whether cases should share a leakage group.

The audit can run before splits exist; cross-split findings are then explicitly
unavailable. An existing malformed or incomplete split manifest is rejected.
Zero/one-case skills report insufficient cases. Empty token vectors retain
exact-input checks but have unavailable cosine scores.

All-splits reports and cached vectors are curation evidence. They must remain
outside training-author, solver, optimizer, and model-selection inputs. No
automatic recorder projection is performed. Local content-hash verification
does not reacquire source snapshots or establish independent acceptance.

See [ADR-014](../architecture/014-case-similarity-audit.md) for the algorithm,
identity rules, alternatives, and validation requirements.
