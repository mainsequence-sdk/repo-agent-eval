# ADR-014: Case Similarity Audit Within a Skill

- Status: Accepted and implemented (offline TF-IDF and exact-input audit)
- Date: 2026-10-14
- Decision owners: MS Agent Eval maintainers
- Scope: case-bank authoring audits, exact duplicates, cosine similarity,
  leakage-group review, reproducible reports, and future embedding support
- Related: [Capability coverage](../evaluation/capability-coverage.md),
  [data splits](../training/data-splits.md), and
  [ADR-003](003-content-addressed-experiment-graph.md)

## Context

The framework validates case packages, provenance, capability coverage, and
declared leakage-group assignments. It does not compare the content of cases
assigned to different groups. Consequently, distinct case IDs and sufficient
group counts can conceal repeated tasks, including tasks appearing in both
train and test.

A read-only inspection on 2026-10-14 found 452 cases across 20 skills in the
MainSequence SDK workspace. Comparing solver-view hashes within each skill
identified 34 identical-input pairs. Sixteen crossed permanent splits,
including seven train/test pairs; all sixteen also had identical expected
response text. These are observations of that case-bank revision, not fixed
acceptance counts for the proposed utility.

For example, the cases
`data-publishing-meta-tables-integration-01-construction` and
`data-publishing-meta-tables-management-mode-selection-construction` have
different group IDs assigned to train and test respectively. A content audit
can expose repetition that structural group validation cannot detect.

Cosine similarity requires a vector representation. Word overlap and semantic
similarity are different signals, and neither establishes independent coverage
or solution equivalence by itself. The representation and compared case
content must therefore be explicit in every result.

## Decision

Add a standalone, read-only case audit that compares every unique pair within
each selected skill. Start with exact-input hashes and offline TF-IDF cosine
similarity. Keep vectorization separate from pair calculation so a later
embedding implementation can reuse the same audit and reporting contract.

The audit is an authoring diagnostic. It does not grade solver responses,
replace the LLM judge, change readiness rules, edit cases, merge groups, or
reassign splits. A finding leads to an explicit case-bank review and, if
needed, a separately reviewed revision with updated provenance.

### Discovery and validation

Load `WorkspaceRepository` and use `CaseBank.discover(require_cases=False)` with
case-content verification enabled. Select cases by the exact `case.skill` value, including
nested identifiers such as `data_publishing/meta_tables`. Validate the selected
skill against the local capability catalog; do not infer it from folder names.

Do not call `compile_workspace()`: the audit does not need repository snapshot
acquisition, model credentials, judge-validation fixtures, or complete coverage
depth. Loading local packages verifies their recorded content hashes; it does
not certify their immutable source provenance or full evaluation readiness.
Malformed packages and mismatched content hashes remain errors.

Load a complete split manifest through the existing `SplitManifest` when one
exists. If it is absent, allow a content-only audit and mark split assignments
and cross-split findings as unavailable. An existing invalid or incomplete
manifest is an error, not permission to infer assignments or weaken the scored
workflow's split validation.

An unknown skill is an error. A catalogued skill with zero or one case produces
an explicit insufficient-cases result with no pair scores. The default corpus
contains all permanent splits for the selected skill.

### Compared content

Keep two independent views:

| View | Included content | Purpose |
| --- | --- | --- |
| `solver` | Prompt and solver-visible text attachments | Compare the task and supplied evidence |
| `expected` | Expected response and expected text artifacts | Compare reference solutions |
| `both` | Both views, calculated separately | Inspect task and solution overlap together |

Default to `solver`. Exclude case IDs, titles, groups, capability labels,
provenance, source paths, rubrics, and shared skill/global instructions from
vectorized text. Retain classification fields as report metadata.

Render the main text followed by attachment bodies in sorted relative-path
order, with deterministic separators. File paths determine ordering and exact
view identity but are not vectorized as task prose. Use the existing text
artifact readers and reject unreadable content; do not silently omit files.
Record the renderer version and do not truncate long inputs silently.

Use `solver_view_hash` for exact solver-input equality. Define a separate
expected-view hash over response text and expected artifacts. The existing
`judge_view_hash` includes the prompt and rubric, so it is not the identity of
the proposed expected-only view. A cosine score of one is not an exact-content
flag: distinct texts can yield the same vector.

### TF-IDF and cosine calculation

The initial representation uses Unicode-casefolded word unigrams, preserving
digits and underscores within tokens, with a fixed minimum token length of
two. Use no stopword list, stemming, identifier replacement, or inferred
boilerplate removal. Record the tokenizer, weighting, and normalization version.

Fit each view independently on all selected cases, including repeated texts.
For token count `c`, document frequency `df`, and corpus size `N`, use:

```text
tf(c) = 1 + ln(c), for c > 0
idf(t) = 1 + ln((N + 1) / (df(t) + 1))
weight(t, case) = tf(c) * idf(t)
cosine(a, b) = dot(a, b) / (norm(a) * norm(b))
```

Normalize each nonzero vector once. Compare each pair once using stable case-ID
ordering, excluding self-pairs and mirrored pairs. Preserve full calculation
precision in stored scores; round only for display.

Empty token vectors produce unavailable cosine scores with a reason, not a
fabricated zero. Exact-input comparisons remain available. Reject nonfinite
or inconsistent vector output. TF-IDF scores fall in `[0, 1]`; a future dense
embedding representation may produce negative cosine values and must retain
them rather than force the same range.

Shared task templates can dominate lexical similarity, while paraphrases can
score poorly. Report this limitation beside the representation description.
Scores from different representations or differently fitted corpora are not
interchangeable. Do not choose an automatic duplicate threshold without
calibration against reviewed case pairs.

### Command and report contract

The command operates on one exact skill, repeated `--skill` selections, or
every catalogued skill with `--all-skills`:

```bash
ms-agent-eval cases similarity \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --skill data_publishing/meta_tables \
  --representation tfidf \
  --view both \
  --top-k 10
```

`--top-k` limits retained nearest neighbors per case and per view, not the
computed corpus or pair set. Break equal-score ties by case ID. The neighbor
CSV also includes the twenty highest-scoring pairs per comparison category
and view, exposing cross-split findings even when closer same-group neighbors
exist. Deduplicate this union by case pair. Summary JSON and Markdown expose
the same aggregate findings; stdout JSON gives counts, identities, and report
paths. `--json` suppresses stderr progress. `--export-pairs` additionally streams
the full unique-pair table to CSV. Heatmaps remain a possible extension.

Each pair row records both case IDs, available similarity scores, exact-view
equality flags, groups, splits, primary capabilities, and archetypes. Report
same-group, different-group, and cross-split comparisons separately, with exact
cross-split duplicates prominently identified. Missing split evidence must not
be represented as a negative leakage finding.

The summary records the selected skill, case count, total unique pairs,
scorable pairs per view, unavailable scores, exact-duplicate counts, and
cross-split findings. Full exports contain each unique pair once, even when
both cases select each other as nearest neighbors. Keep raw prompts and
expected answers out of summary rows by default.

All similarity findings are advisory. Successful analysis
does not mean the bank is diverse or leakage-free. Automatically failing
workspace validation, including for exact duplicates, would require a separate
decision about readiness and migration of existing banks.

### Held-out data boundaries

An all-splits audit belongs to case-bank curation. Its report, vectors, and
reference-solution comparisons must not enter solver inputs, demonstrations,
optimizer feedback, or model selection. This includes reports containing only
case IDs and scores: they can still expose relationships to held-out tasks.

Keep audit artifacts outside run-stage input construction and exclude them
from automatic recorder projection. Review involving test or challenge cases
must remain separate from training-author and optimization sessions, following
the workspace's authoring policy. The utility does not create new filesystem
access controls; its architecture must preserve the existing stage boundaries.

### Storage and cache identity

Use the configured external workspace data root and `FilesystemArtifactStore`.
Store an immutable audit manifest under
`operations/manifests/case-similarity/<audit-id>.json`, referenced vector/result
blobs under the existing content-addressed blob store, and derived readable
exports under `summary/case-similarity/<audit-id>/<report-id>/`. Generated
evidence stays outside Git. Manifest references keep blobs reachable during
garbage collection. Vector-cache, report-projection, and multi-skill bundle
manifests use the sibling `case-similarity-vectors`, `case-similarity-reports`,
and `case-similarity-bundles` namespaces. A bundle has a separate report index
under `summary/case-similarity/<bundle-id>/`.

By default, retain vector caches, aggregate score histograms, bounded pair
rankings, and exact-duplicate families. Every pair is calculated, but only
`--export-pairs` retains every pair score in a full table. Changing display
options may recalculate pairs from cached vectors.

The audit identity includes sorted case identities and package hashes, catalog
and split identities or explicit missing-split state, selected skill, views,
renderer, and representation settings. Display limits identify a report
projection separately; changing `top-k` must not invalidate calculated vectors.

TF-IDF vector identity includes the complete selected corpus and fit settings.
Adding a case can change IDF and therefore invalidate existing vectors even
when individual text has not changed. Metadata-only edits may reuse vectors
but must regenerate the audit's group, split, and coverage annotations.

Future embedding cache keys must identify the exact rendered input, provider,
model revision, dimensions, preprocessing, and any chunking or pooling policy.
Embedding transport, credentials, budgets, and request evidence require an
explicit design before that representation is enabled; chat-model configuration
does not establish embedding support.

## Implementation boundaries and scale

Add a focused `core/case_similarity.py` module for rendering, vectorization,
pair calculation, and audit result types. Wire the command through `core/cli.py`
and reuse case loaders, hashing, and external storage. Keep similarity logic
out of the judge and optimizer. No case or workspace schema change is needed
for the initial command's explicit options.

Declare any directly imported numerical or vectorization dependency explicitly
instead of relying on optional or transitive installations. The initial
implementation needs no model calls, model downloads, or vector database.

For `n` cases there are `n * (n - 1) / 2` pairs. The inspected MainSequence bank
requires 5,012 within-skill pairs across all twenty skills. The subsequent
MetaTables bank has 5,000 cases per skill: 12,497,500 pairs each, or 62,487,500
across its five skills. Comparing both views calculates two scores per pair.
Use sparse vectors and upper-triangle blocks (default 256 cases per block);
top-k reporting reduces output size but does not remove quadratic comparison
work. Do not introduce approximate search without explicitly changing the
all-pairs contract.

## Alternatives and consequences

- **Exact hashes only:** inexpensive and useful immediately, but unable to
  surface near-duplicates. Retain them alongside cosine scores.
- **Embeddings first:** better suited to paraphrase review, but adds model
  selection, inference infrastructure, costs, and revision-sensitive caching.
  Defer until the offline audit contract is established.
- **One vector for the entire case package:** hides whether similarity comes
  from the task, answer, rubric, or shared metadata. Use separate content views.
- **Automatic group merging or split repair:** similarity is not a reliable
  equivalence relation, and edits affect provenance and benchmark membership.
  Keep remediation an explicit review operation.
- **Embedding the audit in every evaluation run:** couples authoring diagnostics
  to scoring and exposes held-out relationships to the runtime unnecessarily.
  Keep the initial command independent.

The audit complements capability coverage and declared group integrity. It
cannot prove semantic novelty, independent solutions, complete skill coverage,
or absence of cross-skill leakage; its initial scope is within one skill.

## Acceptance criteria

Implementation should demonstrate:

1. Exact skill filtering, verified package loading, and useful zero/one-case
   results without snapshots, credentials, or full coverage readiness.
2. Correct known cosine results, symmetric scores, deterministic pair ordering,
   explicit empty-vector handling, and retained exact-duplicate flags.
3. Independent solver and expected views, including text artifacts, with no
   metadata or shared-instruction contamination.
4. Accurate same-group and cross-split findings, an explicit missing-split state,
   and errors for malformed supplied manifests.
5. Exhaustive pair counts independent of top-k, stable ties, and equivalent
   results from blocked and unblocked calculation.
6. Corpus-sensitive TF-IDF cache invalidation, updated metadata annotations,
   reproducible report identities, and externally stored artifacts reachable
   through manifests.
7. Synthetic CLI integration covering duplicate inputs in different groups and
   splits, with no case edits, network calls, solver dispatch, or optimizer
   access to audit artifacts.

The offline command is implemented and covered by focused numerical, case-view,
CLI, split, cache, and artifact-reachability tests. Embedding inference and
automatic readiness enforcement remain outside this decision. See the
[case similarity guide](../evaluation/case-similarity.md) for usage and outputs.
