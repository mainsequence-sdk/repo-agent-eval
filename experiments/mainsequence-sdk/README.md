# MainSequence SDK Evaluation Workspace

This is the complete version-controlled template for evaluating the public
MainSequence SDK at tag `v8.1.12`. It contains configuration and authored
evaluation inputs only. Repository snapshots, DSPy calls, runs, and
compiled programs go to `~/ms_agent_eval/mainsequence-sdk-evaluation`.
Completed and in-progress runs are also projected to the configured local
MLflow server at `http://192.168.1.17:5000`; the external filesystem remains the
source of truth.

## Part A — evaluation target

`workspace.yaml` points to the normative evaluation target:

- `https://github.com/mainsequence-sdk/mainsequence-sdk` at `v8.1.12`;
- global instructions at `agent_scaffold/AGENTS.md`;
- every recursively discovered `agent_scaffold/skills/**/SKILL.md`;
- nine platform-owned skills imported from the authenticated MCP catalog and
  locked under `instructions/mainsequence-platform/`;
- versioned Codex authoring policy at `case-authoring.md`;
- authored cases under `cases/`; and
- the DSPy solver and rubric-judge contracts.

Codex authoring also uses six independently resolved reference
repositories:

- the illustrative beginner tutorial at `v0.1.67`;
- MainSequenceMarkets at `v1.0.14`, used for MetaTable architecture,
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
`references/<source-id>/...`. SDK `v8.1.12` remains authoritative for Python SDK
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

The instruction inventory contains 20 skills: 11 SDK-owned skills from the
pinned Git release and nine platform-owned skills from MCP. The completed bank
contains 452 cases across 236 semantic leakage groups and covers all 193
catalogued capabilities. Every skill has at least 20 cases, at least 10 groups,
and at least two primary cases for every capability. Skills with more than ten
capabilities exceed the floor. These are coverage gates, not a claim that this
volume alone is sufficient for a production fine-tune.

Automatic deployment is deliberately over-sampled. Separate construction and
review families cover standalone Jobs, workflow Jobs, workflow FastAPI
releases, workflow runtime Agent releases, workflow Static Sites, and workflow
CodeRepository Coding Agents. Additional cases test target-specific image/build
ownership, automatic redeployment, and the difference between declaration,
DeploymentRun, health, and representative behavior evidence.

The imported MCP manifest records the `v8.1.12` retrieval client, resolved SDK
commit, MCP protocol version, platform manifest and ontology hashes, and every
resource URI/path/content hash. Runs consume these committed inputs and never
refresh MCP implicitly. The current catalog has nine skills and intentionally
does not retain the retired `command_center_connections` skill. It also does not
expose the former `command_center` skill; neither stale skill is reconstructed
from SDK tests or older snapshots.

`case-authoring.md` is the long-lived
policy that tells Codex how to ground cases, construct solver-facing
prompts and reference answers, design LLM-judge rubrics, and prevent split
leakage. It distinguishes documentation/tutorial-grounded work from cases
grounded in the project portfolio without assigning subjective difficulty
labels. Completed reports classify exact expected-response token length. It
also records where the projects
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
Codex writes at least 20 cases in at least 10 groups per selected skill and
continues until every capability in `cases/coverage.yaml` has two primary
examples. Schema-v6 cases
separate the public `solver/` surface from private `judge/` evidence and record
the authoring-policy, capability-catalog, source-set, and package hashes.

Reviewed train cases may later feed DSPy demonstrations,
distillation, or fine-tuning exports. Those exports stay outside Git and must
retain source-set and leakage provenance. Test and challenge prompts, answers,
responses, and judge feedback are never training material.

## Part B — experiments

The same manifest defines:

- `baseline`, which evaluates the typed DSPy solver on the untouched test split;
- `optimize-solver`, which selects `strategy: copro`, keeps the solver
  zero-shot, and searches the solver instruction, response prefix, and all
  configured source/imported skill overlays. Each proposal is evaluated by the
  solver and separate LLM judge inside grouped cross-validation before the
  selected bundle is compiled from train. The frozen original and optimized
  programs are then scored on the same untouched test cases for a paired report.

Executor definitions, model names, and stage assignments are stored directly in
`workspace.yaml`. Optimizer, solver, and judge are separately budgeted roles
and may intentionally use the same provider/model pair.

Every model also declares a `deployment_revision`, and every stage declares an
explicit reuse policy. This workspace uses `exact`: an unchanged validated
judge validation, fitted state, solver response, judgment, selection, or aggregate is
attached to the new run instead of executed again. Update the Qwen deployment
revision whenever its weights, quantization, tokenizer, chat template, or
server behavior changes. The terminal marks these attachments with `↺`; the
run report separates executed and reused work. See
[incremental execution](../../docs/execution/incremental-execution.md).

## Bootstrap and run

The included `workspace.yaml` uses `gpt-5.6-sol` for case authoring and
`qwen3.6:27b-coding` for the optimizer, solver, and judge stages. No environment
variables are required by its unauthenticated local API configuration. The
solver uses the model's default reasoning level; the judge sets the first-class
`reasoning_effort: none` so structured rubric output is not consumed by a hidden
thinking trace. Its `lan-inference` executor combines two request slots on each
of the two `192.168.1.17` llama.cpp servers and four slots on the
`192.168.1.10` llama server, for eight requests total. All three endpoints must
serve the deployment named by the shared Qwen deployment revision. For an
authenticated provider, configure
`api_key_env` on the applicable endpoint item and keep only the credential
value in `.env`.
The workspace enables `live_and_finalize` MLflow recording with full evidence.
Install it with `uv sync --extra mlflow`, start the tracking server outside Git,
and run `ms-agent-eval recorder doctor` before inference. An unavailable MLflow
server leaves retryable pending delivery and does not fail the benchmark. See
[experiment recording](../../docs/execution/experiment-recording.md).
The author stage is intentionally `artifact_exchange`: the active Codex session
authors and reviews files in Git. To automate it or any other model-driven stage
with subscription-backed Codex, configure the isolated `codex_cli` executor in
the [harness guide](../../docs/execution/codex-cli.md); it never uses API-billed
inference.
Review `case-authoring.md` before authoring cases; changing it invalidates the
authoring provenance of affected cases. Then:

```bash
uv run ms-agent-eval validate \
  --workspace experiments/mainsequence-sdk/workspace.yaml

# In Codex: use the case-authoring skill for this workspace and ask for a
# precise campaign, such as two foundational cases for every selected skill.
```

Run multiple precise campaigns per skill rather than one generic campaign per
skill. The minimum portfolio uses five foundational applications, five
boundary/misconception cases, five project-backed integration or architecture
cases, and five review/schema-evolution/evidence-limit cases. If no real defect
supports a diagnostic case, use review or evidence-boundary work; never invent
a defect to fill a quota. Across the accepted cases, use at least ten semantic
leakage groups and at least three applicable archetypes. The policy contains
routing for all 20 configured skills and an explicit deployment-target matrix.

Every package remains one atomic case for validation and provenance. Repeat
Codex authoring requests with materially different coverage goals until
`validate` reports no per-skill coverage gaps. The same Codex authoring campaign
must create or update the controlled `strong`, `partial`, `incorrect`,
`contradictory`, and `adversarial` candidates under `judge-validation/`, each
referencing a reviewed training case. Do not leave judge validation as a
manual follow-up. Review the complete authoring deliverable, then validate until
`ready_for_scored_run` is `true`:

```bash
uv run ms-agent-eval inspect \
  --workspace experiments/mainsequence-sdk/workspace.yaml
# Diagnostic execution while coverage remains incomplete:
uv run ms-agent-eval run baseline --pilot \
  --workspace experiments/mainsequence-sdk/workspace.yaml
# Scored execution after the readiness gate passes:
uv run ms-agent-eval run baseline \
  --workspace experiments/mainsequence-sdk/workspace.yaml
uv run ms-agent-eval run optimize-solver \
  --workspace experiments/mainsequence-sdk/workspace.yaml
```

The optimization command validates the judge, runs bounded COPRO search inside
each train-only outer fold, records out-of-fold diagnostics, repeats search on
all train cases, freezes the selected instruction/prefix/skill overlays as a
zero-demonstration compiled artifact, and only then loads test cases. Optimizer
packets never contain rubrics, expected responses, judge feedback, or held-out
inputs.

`validate` and `inspect` make no LLM requests. The five-fixture judge validation
corpus is configured and uses the train-split
`metatable-related-model-graph` case. The 452-case bank passes source,
authoring-provenance, capability-depth, per-skill case/group, grouped-split,
runtime-stage, and judge-validation readiness checks. A normal `run` is scored.
For a normal run, the five fixtures and default single judge repetition form five
independent validation votes. They are dispatched across the configured
8-request endpoint pool and then aggregated in stable fixture/repetition order.
Multiple judge repetitions are opt-in for experiments that explicitly need a
consensus robustness study.
`run --pilot` remains the explicit exception for future incomplete revisions:
it runs only train cases, never optimization or held-out splits, uses one
diagnostic judge vote per fixture/case, records `benchmark_valid: false`, and
omits aggregate benchmark metrics.
