---
name: mainsequence-experiment-version
description: Update the MainSequence SDK example workspace to a new public tag or commit, audit its SDK-owned and hash-pinned MCP instruction inventory, and revalidate affected cases without committing generated snapshots or run artifacts.
---

# MainSequence Experiment Version

Update `experiments/mainsequence-sdk/` while keeping the generic framework
independent from the evaluated SDK.

## Invariants

- Use the HTTPS repository URL and immutable tag or full commit configured in
  `workspace.yaml`, never an installed package or mutable checkout.
- Select exactly the configured global instructions and SDK-owned skill
  inventory. Import platform-owned instructions only through a
  workspace-declared manifest that records the MCP manifest, ontology,
  resource paths, and content hashes.
- Never reconstruct a missing or retired platform skill from SDK tests, docs,
  memory, or an older MCP import.
- Keep snapshots, drafts, calls, runs, reports, and fitted programs under the
  external data root; never commit them.
- Treat the reviewed MCP manifest and exact imported instruction files as
  versioned experiment inputs, not generated run output.
- Do not add the evaluated SDK as a framework dependency.
- Preserve unrelated worktree changes.

## Workflow

1. Update the target ref in `workspace.yaml` and resolve it to an immutable
   snapshot. When MCP refresh is requested, retrieve the catalog through that
   exact SDK revision and replace the complete import instead of merging it
   with stale files.
2. Audit the selected global instructions, complete SDK and platform skill
   inventory, import manifest, and capability catalog against the new
   snapshots.
3. Identify changed or removed APIs and the cases whose prompts, references,
   rubrics, coverage, or source paths depend on them.
4. Rebuild or review affected cases using the `case-authoring` skill. Recompute
   authoring-policy, catalog, source-set, and case-content provenance hashes.
5. Review schema-v3 `cases/splits.yaml`. Preserve leakage groups; permanent
   splits are only `train`, `test`, and optional `challenge`.
6. Review the schema-v3 `judge-validation/` set when a referenced case or rubric
   changed. Fixtures use training cases only and carry ordinal labels without
   score ranges.
7. Run static validation and report the new tag/commit, snapshot hash, MCP
   manifest and ontology hashes, SDK/platform instruction changes, affected
   cases, split changes, judge-validation changes, validation result, and
   remaining blockers.

Optimization derives grouped K-fold cross-validation inside `train`, selects
the highest mean out-of-fold candidate, refits it on all training cases, and
evaluates the untouched test set only after selection. COPRO then compares the
frozen original and optimized programs on identical test cases. Never use test or challenge content or
results to revise cases, demonstrations, or hyperparameters.
