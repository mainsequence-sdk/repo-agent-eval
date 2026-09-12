# MS Agent Eval Documentation

MS Agent Eval is a Python 3.12+ framework where Codex authors grounded cases and
two distinct DSPy LLM roles solve and judge them.

## Start here

1. [Getting started](getting-started.md) — complete repository-to-result flow.
2. [Repository structure](structure.md) — what is versioned and what remains external.
3. [Framework conventions](conventions.md) — identities, provenance, judging, and splits.
4. [OpenAI-compatible API](openai-compatible-api.md) — configure the solver and judge.
5. [Target source workflow](target-source-workflow.md) — immutable target and
   case-authoring reference snapshots.

## Architecture

- [Historical three-LLM proposal](implementation_tasks/017-dspy-only-workspace-ux.md)
- [Guided CLI and OpenAI-compatible API](implementation_tasks/018-guided-cli-and-openai-compatible-api.md)
- [Versioned case-authoring instructions](implementation_tasks/019-versioned-case-authoring-instructions.md)
- [Diagnostic error-identification criterion](implementation_tasks/020-diagnostic-error-identification-criterion.md)

## Repository-specific examples

Examples demonstrate how a target repository consumes the generic framework;
they are not part of the core architecture or required workflow.

- [MainSequence SDK workspace](../experiments/mainsequence-sdk/README.md)
- [Command Center SDK workspace](../experiments/command-center-sdk/README.md)

Numbered files under [implementation_tasks](implementation_tasks/) are the
historical implementation record, not the active architecture. Current behavior
is defined by this documentation, schema-v6 workspaces, and the repository-local
case-authoring skill.

Generated snapshots, model calls, results, compiled programs, and
reports belong under `~/ms_agent_eval/<workspace-id>` or the explicit external
`workspace.data_root`, never in this repository.
