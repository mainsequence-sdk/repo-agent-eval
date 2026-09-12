---
name: case-authoring
description: Use when creating, updating, reviewing, or planning evaluation cases in MS Agent Eval experiment workspaces, including case.yaml, prompts, expected responses, rubrics, case-bank metadata, and cases grounded in immutable repository snapshots or training-source documents.
---

# Case Authoring

## Purpose

Use the current Codex session as the case-authoring agent. Read the versioned
experiment policy and immutable repository evidence, then create reviewed case
packages directly in the experiment case bank. Case authoring is not a DSPy
program and does not call a configured OpenAI-compatible backend model.

The runtime has exactly two LLM roles: the solver answers cases and the judge
scores those answers. Keep both roles and all evaluation results isolated from
authoring.

## Read First

Read the workspace's `workspace.yaml`, its configured
`evaluation.case_authoring.instructions` file, `README.md`,
`docs/getting-started.md`, `docs/structure.md`, and `docs/conventions.md`.
Read the configured `evaluation.cases.coverage.catalog` before selecting any
case. A draft or incomplete catalog is an authoring blocker, not permission to
sample an arbitrary subset of a skill.
Resolve the configured target and every `evaluation.case_authoring.sources`
repository to immutable external snapshots before considering case content.

The skill paths are exactly the `evaluation.instructions.skills` selection in
the manifest. Never guess `.agents/skills`, `agent_scaffold/skills`, or another
location. If `directory` is configured, use every recursively discovered
`SKILL.md`. If `files` is configured, use exactly those files.

## Non-negotiable Rules

- The active Codex session authors the cases; never require a builder endpoint,
  builder model name, builder credential, or builder DSPy configuration.
- Treat the configured authoring file as durable policy and the user's request
  as the narrower campaign objective.
- Treat every requested case count as a lower bound. Author at least three
  materially distinct cases per selected skill and expand further whenever the
  capability catalog has unmet primary-coverage requirements.
- Before authoring, decompose each complete selected skill into capability
  units from its purpose, required decisions, numbered build rules,
  prohibitions, review rules, stop conditions, and required supporting docs.
  Record those units in the capability catalog; never infer breadth from case
  count alone.
- Give each case with exactly one cataloged primary capability and no more than
  two supporting capabilities. A capability is covered only by primary cases;
  supporting tags provide context but not readiness credit.
- Ground cases in the immutable target/reference source set, not an installed
  package, mutable checkout, branch, or model memory.
- Preserve reference labels exactly as
  `references/<source-id>/<repository-path>` and bind provenance to the combined
  authoring source-set hash.
- Never use solver responses, judge votes, optimizer traces, scores, or held-out
  feedback during authoring.
- Never send a rubric or expected response to the solver.
- When a case contains a grounded faulty test, false premise, broken
  configuration, contradiction, or misleading symptom, require an
  `error_identification` criterion weighted at 0.35–0.50. Do not add it when no
  real defect is present.
- Do not add evaluator methods, evaluator status fields, Python judge plugins,
  keyword checks, or checklist scoring code. The DSPy judge LLM is the only
  semantic judge.
- Assign leakage groups semantically. Apply the configured deterministic split
  policy after cases exist; never choose or change a group to obtain a desired
  train/development/test/challenge placement.
- Do not copy source repositories, model calls, or run results into Git.

## Workflow

### 1. Inspect the locked workspace

Run:

```bash
ms-agent-eval inspect --workspace path/to/workspace.yaml
```

Confirm every source URL/ref/commit/snapshot, the combined source-set hash,
global instructions, exact skill ids and paths, case coverage, split groups,
solver identity, judge identity, and calibration corpus. An incomplete status
caused only by missing cases or calibration is expected while authoring.

### 2. Build or audit the capability catalog

Read every selected skill completely. Create one stable capability id for each
independently testable decision or behavior, merge only true semantic
duplicates, and set `minimum_primary_cases` to the number of independent
examples required. The default is three. Mark the catalog `complete` only when
every selected skill has been decomposed. Validation must report all remaining
capability gaps explicitly.

### 3. Read exact evidence

For each requested skill, read the complete locked `SKILL.md`, then the smallest
set of target/reference files needed for the selected capability. Treat target
instructions as normative. References provide scenarios and concrete examples;
they do not redefine the evaluated SDK.

### 4. Author atomic packages

Create this shape directly:

```text
cases/<skill-id>/<case-id>/
├── case.yaml
├── prompt.md
├── expected/
│   ├── response.md
│   └── artifacts/       optional
└── rubric.yaml
```

The prompt must be self-contained for a response-only solver unless the
workspace explicitly selects an isolated execution runtime. The expected
response is judge reference evidence, not an exact-string oracle. The rubric
defines semantic quality and all weights sum to one.

Use case schema version 4. Classify the case explicitly:

```yaml
coverage:
  primary: one-capability-id
  supporting: [optional-capability-id]
  archetype: construction
  difficulty: easy
```

Allowed archetypes are `construction`, `review`, `diagnosis`, `migration`, and
`evidence`; difficulty is `easy` or `complex`. Provenance is:

```yaml
provenance:
  authoring_agent: codex
  authoring_instructions_hash: sha256:...
  capability_catalog_hash: sha256:...
  source_snapshot_hash: sha256:...
  case_content_hash: sha256:...
```

Compute `authoring_instructions_hash` from the exact configured authoring file,
use the inspected combined source-set hash, and compute `case_content_hash` from
the framework's case identity. It is acceptable to write `pending` while
calculating the final package hash, but never leave a promoted case pending.

### 5. Update grouped splits mechanically

For each new leakage group, use the workspace split seed and proportions to
derive its split with the framework's SHA-256 mapping. Preserve existing group
assignments. Related variants share one group and therefore one split.

### 6. Validate the complete contract

```bash
ms-agent-eval validate --workspace path/to/workspace.yaml
```

Validation must prove that every case skill exists, every source path is locked,
every group owns exactly one split, every expected result and rubric exists,
weights sum to one, provenance matches current package bytes and authoring
policy/catalog, every required capability has enough primary cases, solver and
judge identities differ, and judge calibration has strong,
partial, incorrect, contradictory, and adversarial human labels.

## Dataset Governance

Optimization sees train and development only. A compiled state-only JSON
artifact is published before the untouched test set is loaded. Never use test or
challenge results to revise cases, prompts, demonstrations, or optimizer
settings.

Examples from pinned projects may seed derivative synthetic cases only when
their provenance and shared leakage group are retained. Distillation and
fine-tuning exports use reviewed train/development cases only, remain outside
Git, and never contain held-out prompts, answers, responses, or judge feedback.

## Final Response

Report the workspace, resolved source commits and source-set hash, authored case
ids by skill, exact source paths, leakage groups and assigned splits, validation
performed, remaining coverage gaps, and any case rejected during self-review.
