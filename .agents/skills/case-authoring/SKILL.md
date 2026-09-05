---
name: case-authoring
description: Use when creating, updating, reviewing, or planning evaluation cases in MS Agent Eval experiment workspaces, including case.yaml, prompts, expected responses, rubrics, case-bank metadata, and cases grounded in immutable repository snapshots or training-source documents.
---

# Case Authoring

## Purpose

Create grounded schema-v2 evaluation cases through the workspace's configured
DSPy case-builder LLM. Builder requests, responses, rejected drafts, and call
evidence remain external. Only an explicitly promoted, validated case package
belongs in Git.

## Read First

Read the workspace's `workspace.yaml`, its configured
`evaluation.case_builder.instructions` file, `README.md`,
`docs/getting-started.md`, `docs/structure.md`, and `docs/conventions.md`.
Resolve the configured repository ref to its immutable external snapshot before
considering case content.

For MainSequence, the only workspace configuration is:

```text
experiments/mainsequence-sdk/workspace.yaml
```

The skill paths are exactly the `evaluation.instructions.skills` selection in
that manifest. Never guess `.agents/skills`, `agent_scaffold/skills`, or another
location. If `directory` is configured, use every recursively discovered
`SKILL.md`. If `files` is configured, use exactly those files.

## Non-negotiable Rules

- Invoke `ms-agent-eval cases build`; do not directly invent a promoted case.
- Treat the configured authoring file as durable DSPy builder policy and
  `--coverage` as the narrower request for one invocation.
- The builder model must be distinct from the solver and judge models.
- Ground builder inputs in the immutable snapshot, not an installed package or
  an unpinned checkout.
- Never provide solver responses, judge votes, optimizer traces, test scores, or
  held-out feedback to the builder.
- Never send a rubric or expected response to the solver.
- When a case contains a grounded faulty test, false premise, broken
  configuration, contradiction, or misleading symptom, require an
  `error_identification` criterion weighted at 0.35–0.50. Do not add it when no
  real defect is present.
- Do not add evaluator methods, evaluator status fields, Python judge plugins,
  keyword checks, or checklist scoring code. The configured DSPy judge LLM is
  the only semantic judge.
- A builder draft cannot select its train/development/test split. It proposes a
  leakage group; the workspace split policy assigns that group on promotion.
- Do not copy checkouts, model calls, drafts, or run results into Git.

## Workflow

### 1. Inspect the locked target

Run:

```bash
ms-agent-eval inspect --workspace path/to/workspace.yaml
```

Confirm the resolved commit, global instruction paths, discovered skill id and
path, case coverage, split groups, builder identity, judge identity, and
calibration corpus.

### 2. Request builder drafts

Confirm that the three role models and any credential references are configured
in `workspace.yaml`, then invoke the configured builder:

```bash
ms-agent-eval cases build \
  --workspace path/to/workspace.yaml \
  --skill skill/id \
  --coverage "Describe the behavior and coverage gap"
```

The DSPy builder must return a complete package: metadata, prompt, expected
response, optional expected artifacts, weighted rubric, hard failures, immutable
source paths, and leakage group. Its call evidence and draft live below the
external data root.

### 3. Inspect before promotion

```bash
ms-agent-eval cases inspect-drafts --workspace path/to/workspace.yaml
```

Review grounding, self-containment, expected behavior, rubric usefulness,
hard-failure semantics, and leakage grouping. For diagnostic cases, confirm the
prompt contains enough evidence to discover the error and that identifying the
actual root cause receives substantial credit. Reject weak drafts by leaving
them external. Do not repair a rejected draft directly in the case directory;
issue a clearer builder coverage request.

### 4. Promote explicitly

```bash
ms-agent-eval cases promote \
  --workspace path/to/workspace.yaml \
  --draft DRAFT_ID
```

Promotion writes this shape:

```text
cases/<skill-id>/<case-id>/
├── case.yaml
├── prompt.md
├── expected/
│   ├── response.md
│   └── artifacts/       optional
└── rubric.yaml
```

`case.yaml` records builder model/program, source snapshot, generation request,
and package hashes. Direct edits invalidate that provenance and require a new
builder revision. The builder program hash includes the exact authoring-policy
content; after that file changes, rebuild and explicitly promote affected cases
instead of bypassing the mismatch.

### 5. Validate the whole contract

```bash
ms-agent-eval validate --workspace path/to/workspace.yaml
```

Validation must prove that every case skill exists, every referenced source path
is locked, every group owns exactly one split, every expected result and rubric
exists, all rubric weights sum to one, provenance matches package bytes, all
three LLM identities differ, and the judge calibration corpus has strong,
partial, incorrect, contradictory, and adversarial human labels.

## Dataset Governance

Optimization sees train and development only. A compiled state-only JSON
artifact must be published before the untouched test set is loaded. Never use
test or challenge results to revise builder cases, prompts, demonstrations, or
optimizer settings.

## Final Response

Report the workspace, resolved source commit, skill, coverage request, external
draft ids, promoted case ids, source paths, leakage groups, validation performed,
and any rejected drafts or calibration gaps. Never report a draft as promoted.
