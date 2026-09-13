# Command Center SDK Evaluation Workspace

This workspace evaluates the public TypeScript/React Command Center SDK at tag
`v0.1.21`. It contains only versioned experiment configuration and authoring
policy. Repository snapshots, DSPy calls, runs, compiled programs, and
reports belong under `~/ms_agent_eval/command-center-sdk-evaluation`.

## Part A — evaluation target and authoring sources

The normative target configured in `workspace.yaml` is:

- repository: `https://github.com/mainsequence-sdk/command-center-sdk`;
- tag: `v0.1.21`;
- resolved commit: `d9e3f99496daf92c49da362605030bec70e1917d`;
- global instructions: `AGENTS.md` and `command-center-sdk/AGENTS.md`;
- skills: every recursively discovered
  `command-center-sdk/agent_scaffold/skills/**/SKILL.md`; and
- durable Codex authoring instructions: `case-authoring.md`; and
- draft capability catalog: `cases/coverage.yaml`.

The easy-case scenario source is the React/Vite tutorial:

- repository:
  `https://github.com/mainsequence-projects/command-center-sdk-tutorial`;
- tag: `v0.1.30`;
- resolved commit: `e3ba635592425dd8f446cc2233f829930a370a70`.

The SDK is authoritative. `workspace.yaml` configures the tutorial as an
immutable authoring reference, exposed under
`references/tutorial/...`. The tutorial is illustrative and currently pins a
vendored SDK `0.1.17` package, so a tutorial behavior may be used only after it
is confirmed against the evaluated SDK `0.1.21`. Tutorial-backed cases also
require that exact immutable tutorial snapshot; the authoring policy forbids
reconstructing it from memory or a mutable local checkout.

The SDK itself is an ESM TypeScript/React library and is bundler-independent.
The tutorial is a React 19 and Vite 8 consumer application. Cases must not
confuse the tutorial's build system with an SDK requirement.

The case bank intentionally starts empty. Easy cases will be grounded in the
SDK documentation and tutorial. Complex cases will instead be grounded in
separately pinned real projects and executed only in a digest-pinned Node
container, with browser automation added only when visible behavior matters.

## Part B — evaluation experiments

The schema-v11 workspace requires a deployment revision for every model and an
explicit reuse policy for every stage. Replace the `*-configured-v1`
deployment placeholders when real model servers are selected, and bump them
whenever weights or serving behavior changes. Exact validated stage results
are then reusable across runs and compatible experiment revisions; see
[incremental execution](../../docs/execution/incremental-execution.md).

The same manifest defines:

- `baseline`, which asks the DSPy solver to answer each reviewed case and has a
  separate DSPy judge stage score the result semantically; and
- `optimize-few-shot`, which compares the baseline and configured few-shot
  candidates by grouped K-fold cross-validation on train, refits the selected
  configuration on all training cases, and evaluates the untouched test once.

Solver and judge are distinct roles but may use the same configured model.
Executor addresses, model names, and stage assignments are durable workspace
configuration. Only an optional secret belongs in `.env`.

## Prepare the workspace

Review `case-authoring.md` and replace the two runtime placeholder model names in
`workspace.yaml`. Structural validation does not call an LLM:

```bash
uv run ms-agent-eval validate \
  --workspace experiments/command-center-sdk/workspace.yaml

uv run ms-agent-eval inspect \
  --workspace experiments/command-center-sdk/workspace.yaml
```

Ask Codex to use the repository-local `case-authoring` skill and request narrow
cases one skill and behavior at a time. First it must decompose all 24 selected
skills in `cases/coverage.yaml` and mark the catalog complete. Every requested
count is a lower bound and every catalog capability requires at least three
primary cases. Each workspace skill also requires at least 12 accepted cases
across 6 semantic leakage groups; one package is only the atomic review unit:

```text
Use the case-authoring skill with experiments/command-center-sdk/workspace.yaml.
Create three easy diagnostic cases for general/use-command-center-sdk covering
materially distinct React package-boundary failures.
```

Repeat the campaign with different capabilities, sources, archetypes, and
difficulty levels until `validate` reports no capability or per-skill coverage
gaps. The three-primary-case capability depth and 12-case/6-group requirements
are minimums, not a production fine-tuning target.

The same Codex case-authoring campaign must also create or update the controlled
`strong`, `partial`, `incorrect`, `contradictory`, and `adversarial` candidates
under `judge-validation/`; this is not a later manual step. No solver run can
start until the authored case bank, grouped splits, configured runtime stages,
and reviewed judge validation are complete.
