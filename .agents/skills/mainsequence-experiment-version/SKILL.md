---
name: mainsequence-experiment-version
description: Update the MainSequence SDK example workspace to a new public tag or commit, audit the immutable snapshot and instruction inventory, and revalidate affected cases without committing source copies or run artifacts.
---

# MainSequence Experiment Version

Update `experiments/mainsequence-sdk/` while keeping the generic framework
independent from the evaluated SDK.

## Required invariants

- The target is the HTTPS GitHub URL and immutable tag or full commit configured
  in `workspace.yaml`, never an installed package.
- The selected instructions are exactly `agent_scaffold/AGENTS.md` and either
  the configured complete skill directory or explicit `SKILL.md` list.
- Source snapshots and all generated evidence stay under the workspace's
  external data root, which defaults to
  `~/ms_agent_eval/mainsequence-sdk-evaluation`.
- Do not create or commit SDK copies, snapshots, drafts, calls, runs, reports,
  or compiled programs in this repository.
- Do not add `mainsequence` as a framework dependency.

## Workflow

1. Read the root documentation, `experiments/mainsequence-sdk/workspace.yaml`,
   its configured `case-authoring.md`, case bank, split assignments, and
   calibration fixtures. Preserve unrelated worktree changes.
2. Change only `evaluation.repository.ref` to the requested tag or full commit.
3. Run `uv run ms-agent-eval validate --workspace
   experiments/mainsequence-sdk/workspace.yaml`. Record the requested ref,
   resolved commit, snapshot hash, and discovered instruction inventory from
   its output. Validation may report case-provenance blockers after a source
   change; that is expected and must not be bypassed.
4. Compare the new external snapshot with the previous external snapshot when
   one exists. Classify global instructions and skills as unchanged, added,
   removed, renamed, or changed. Do not commit the comparison or snapshot lock.
5. Rebuild cases whose source snapshot provenance is stale with `cases build`,
   inspect their external drafts, and explicitly promote only reviewed drafts.
   Never fabricate builder provenance or edit hashes to make old cases pass.
   If the authoring policy changes, treat its new builder-program hash as an
   intentional case-provenance invalidation and rebuild affected cases.
6. Review group split assignments and human-labelled judge calibration when
   case ids, prompts, expected responses, or rubrics change. Test and challenge
   data must remain held out from optimization and calibration.
7. Run `validate`, `inspect`, Ruff, pytest, lock verification, and a package
   build. Live LLM runs require the user's configured OpenAI-compatible API and
   are separate from structural validation.

## Report

Report the requested ref, resolved commit, snapshot hash, instruction changes,
rebuilt/promoted cases, split or calibration changes, validation results, and
any remaining live-provider gate.
