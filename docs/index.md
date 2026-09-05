# MS Agent Eval Documentation

MS Agent Eval is a Python 3.12+ DSPy framework with three distinct LLM roles:
case builder, solver, and LLM judge.

## Start here

1. [Getting started](getting-started.md) — complete repository-to-result flow.
2. [Repository structure](structure.md) — what is versioned and what remains external.
3. [Framework conventions](conventions.md) — identities, provenance, judging, and splits.
4. [OpenAI-compatible API](openai-compatible-api.md) — configure the three model roles.
5. [Target source workflow](target-source-workflow.md) — immutable repository snapshots.

## Architecture

- [Three-LLM DSPy workspace](implementation_tasks/017-dspy-only-workspace-ux.md)
- [Guided CLI and OpenAI-compatible API](implementation_tasks/018-guided-cli-and-openai-compatible-api.md)
- [Versioned case-authoring instructions](implementation_tasks/019-versioned-case-authoring-instructions.md)
- [Diagnostic error-identification criterion](implementation_tasks/020-diagnostic-error-identification-criterion.md)

## Repository-specific examples

Examples demonstrate how a target repository consumes the generic framework;
they are not part of the core architecture or required workflow.

- [MainSequence SDK workspace](../experiments/mainsequence-sdk/README.md)

Numbered files under [implementation_tasks](implementation_tasks/) are the
historical implementation record. Task 017 is authoritative for the three-LLM
workflow, task 018 for setup UX and model connection configuration, and task 019
for the schema-v4 case-authoring policy contract. Task 020 defines the
high-weight diagnostic error-identification criterion.

Generated snapshots, drafts, model calls, results, compiled programs, and
reports belong under `~/ms_agent_eval/<workspace-id>` or the explicit external
`workspace.data_root`, never in this repository.
