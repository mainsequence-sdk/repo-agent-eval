# MainSequence SDK Evaluation Workspace

This is the complete version-controlled template for evaluating the public
MainSequence SDK at tag `v8.1.9`. It contains configuration and authored
evaluation inputs only. Repository snapshots, DSPy calls, runs, and
compiled programs go to `~/ms_agent_eval/mainsequence-sdk-evaluation`.

## Part A — evaluation target

`workspace.yaml` points to the normative evaluation target:

- `https://github.com/mainsequence-sdk/mainsequence-sdk` at `v8.1.9`;
- global instructions at `agent_scaffold/AGENTS.md`;
- every recursively discovered `agent_scaffold/skills/**/SKILL.md`;
- versioned Codex authoring policy at `case-authoring.md`;
- authored cases under `cases/`; and
- the DSPy solver and rubric-judge contracts.

Codex authoring also uses six independently resolved reference
repositories:

- the illustrative beginner tutorial at `v0.1.67`;
- MainSequenceMarkets at `v1.0.14`, used for complex MetaTable architecture,
  relationship, migration, service, and FastAPI cases;
- Alpaca Connectors at `v0.1.32`, used for concrete provider identity,
  universes, holdings, storage-first bars, Jobs, workflows, and thin APIs;
- Binance Connectors at `v0.1.25`, used for concrete market-data and portfolio
  Jobs, partial migration boundaries, and project-to-agent diagnosis;
- Markets Analytics at `v0.1.10`, used for runtime-attached analytics models,
  `TimeIndexTableRef` consumption, tabular APIs, adapter discovery, and agent
  evidence boundaries; and
- Command Center SDK at `v0.1.21`, used as the authoritative frontend-facing
  resource, discovery, tabular, adapter, and bulk-action contract source.

Each reference is snapshotted outside Git, filtered to the paths declared in
`evaluation.case_authoring.sources`, and exposed under
`references/<source-id>/...`. SDK `v8.1.9` remains authoritative for Python SDK
and MetaTable behavior. The four Python application repositories are project
evidence, while Command Center SDK is contract authority for endpoints consumed
by that frontend. MainSequenceMarkets' older vendored `0.1.13` contract bundle
and Markets Analytics' stale SDK `6.0.46` transfer note are useful only as
migration or diagnostic evidence. Vendored wheels and other binary dependency
copies are excluded from authoring context.

The sibling [`command-center-sdk` experiment](../command-center-sdk/README.md)
evaluates that same `v0.1.21` release directly. This workspace does not read
files from the sibling directory; both experiments independently pin the same
GitHub revision so their provenance remains portable and unambiguous.

The first foundational campaign contains 22 authored and validation-checked
cases: two distinct cases and two leakage groups for each of the 11 configured skills. See
[`cases/README.md`](cases/README.md) for the inventory. This first round is not
the completed bank. `workspace.yaml` requires at least 12 reviewed cases and 6
distinct leakage groups for every one of the 11 skills before the workspace is
ready for a scored run. That produces a minimum bank of 132 cases across at
least 66 semantic families; it is a bootstrap floor, not a claim that the bank
is automatically sufficient for a production fine-tune.

`case-authoring.md` is the long-lived
policy that tells Codex how to ground cases, construct solver-facing
prompts and reference answers, design LLM-judge rubrics, and prevent split
leakage. It separates easy documentation/tutorial-grounded work from complex
cases grounded in the project portfolio. It also records where the projects
meaningfully differ: Alpaca has no local agent entrypoint, Binance and Markets
Analytics contain only generic time-agent examples, and project documentation
can lag the evaluated SDK. Only current API evidence from the pinned target may
enter cases. This workspace authors response-only cases: prompts may include project
excerpts and request textual code or architecture, but they cannot claim edits,
tests, commands, network access, or live-platform verification. Executable
repository cases require a separate future experiment after target execution is
integrated into the scored runner.
For diagnostic cases it also requires a heavily weighted
`error_identification` criterion, so an agent receives substantial credit for
finding the real defect instead of blindly accepting an incorrect test or
premise. A user request defines one campaign slice and the desired number of
atomic, independently reviewable cases per skill. That count is a lower bound:
Codex writes at least three cases per selected skill and continues until every
capability in `cases/coverage.yaml` has three primary examples. Schema-v4 cases
record the authoring-policy, capability-catalog, source-set, and package hashes.

Reviewed train/development cases may later feed DSPy demonstrations,
distillation, or fine-tuning exports. Those exports stay outside Git and must
retain source-set and leakage provenance. Test and challenge prompts, answers,
responses, and judge feedback are never training material.

## Part B — experiments

The same manifest defines:

- `baseline`, which evaluates the typed DSPy solver against every promoted
  case;
- `optimize-few-shot`, which compiles `LabeledFewShot` using train cases,
  measures development cases, publishes state-only JSON, and only then scores
  the untouched test split.

The solver and judge model names are stored directly in `workspace.yaml` and
must identify two different provider/model pairs. They may share one
OpenAI-compatible endpoint. Case authoring has no backend model setting.

## Bootstrap and run

From the repository root, replace the two placeholder `name` values in
`workspace.yaml` with available, distinct runtime models. No environment variables are required by the included
unauthenticated local API configuration. For an authenticated provider, configure `api_key_env` in
the applicable model blocks and keep only the credential value in `.env`.
Review `case-authoring.md` before authoring cases; changing it invalidates the
authoring provenance of affected cases. Then:

```bash
uv run ms-agent-eval validate \
  --workspace experiments/mainsequence-sdk/workspace.yaml

# In Codex: use the case-authoring skill for this workspace and ask for a
# precise campaign, such as two foundational cases for every selected skill.
```

Run multiple precise campaigns per skill rather than one generic campaign per
skill. A good initial portfolio contains at least three foundational
applications, three boundary/misconception cases, three project-backed
integration or architecture cases, and three diagnosis/migration/evidence-limit
cases. If no real defect supports a diagnostic case, replace it with a review or
evidence-boundary case; never invent a defect to fill a quota. Across the 12
accepted cases, use at least six semantic leakage groups and at least three
applicable archetypes. The policy contains routing for all 11 target skills,
but explicit source and capability slices produce better evidence selection.

Every package remains one atomic case for validation and provenance. Repeat
Codex authoring requests with materially different coverage goals until
`validate` reports no per-skill coverage gaps.

Add human-labelled `strong`, `partial`, `incorrect`, `contradictory`, and
`adversarial` responses under `judge-calibration/`, each referencing a promoted
train/development case. Validate until `ready_for_scored_run` is `true`, then:

```bash
uv run ms-agent-eval inspect \
  --workspace experiments/mainsequence-sdk/workspace.yaml
uv run ms-agent-eval run baseline \
  --workspace experiments/mainsequence-sdk/workspace.yaml
uv run ms-agent-eval run optimize-few-shot \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

`validate` and `inspect` make no LLM requests. They currently report the ten
remaining cases and four remaining leakage groups per skill, plus the empty
calibration corpus, as explicit blockers. `run` remains strict and cannot start
the solver until source, authoring provenance, coverage, splits, two runtime
model identities, and judge calibration are complete.
