# MainSequence SDK Evaluation Workspace

This is the complete version-controlled template for evaluating the public
MainSequence SDK at tag `v4.4.5`. It contains configuration and authored
evaluation inputs only. Repository snapshots, DSPy calls, drafts, runs, and
compiled programs go to `~/ms_agent_eval/mainsequence-sdk-evaluation`.

## Part A — evaluation target

`workspace.yaml` points to:

- `https://github.com/mainsequence-sdk/mainsequence-sdk` at `v4.4.5`;
- global instructions at `agent_scaffold/AGENTS.md`;
- every recursively discovered `agent_scaffold/skills/**/SKILL.md`;
- versioned builder policy at `case-authoring.md`;
- promoted cases under `cases/`;
- the DSPy case-builder and DSPy rubric-judge contracts.

The case bank starts empty by design. `case-authoring.md` is the long-lived
policy that tells the builder how to ground cases, construct solver-facing
prompts and reference answers, design LLM-judge rubrics, and prevent split
leakage. For diagnostic cases it also requires a heavily weighted
`error_identification` criterion, so an agent receives substantial credit for
finding the real defect instead of blindly accepting an incorrect test or
premise. The `--coverage` argument is only the specific topic requested for one
builder call. Only a real observed builder call followed by explicit promotion
may create a schema-v2 case; no legacy case is retained and no builder
provenance is fabricated.

## Part B — experiments

The same manifest defines:

- `baseline`, which evaluates the typed DSPy solver against every promoted
  case;
- `optimize-few-shot`, which compiles `LabeledFewShot` using train cases,
  measures development cases, publishes state-only JSON, and only then scores
  the untouched test split.

The builder, solver, and judge model names are stored directly in `workspace.yaml`
and must identify three different provider/model pairs. They may share one
OpenAI-compatible endpoint, including Ollama's `/v1` API.

## Bootstrap and run

From the repository root, replace the three placeholder `name` values in
`workspace.yaml` with installed,
distinct models. No environment variables are required by the included
unauthenticated local API configuration. For an authenticated provider, configure `api_key_env` in
the applicable model blocks and keep only the credential value in `.env`.
Review `case-authoring.md` before generating cases; changing it changes the
builder program identity and requires affected cases to be rebuilt. Then:

```bash
uv run ms-agent-eval validate \
  --workspace experiments/mainsequence-sdk/workspace.yaml

uv run ms-agent-eval cases build \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --coverage "Create grounded cases across every discovered MainSequence skill"

uv run ms-agent-eval cases inspect-drafts \
  --workspace experiments/mainsequence-sdk/workspace.yaml

uv run ms-agent-eval cases promote \
  --workspace experiments/mainsequence-sdk/workspace.yaml \
  --draft DRAFT_ID
```

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

`validate` and `inspect` make no LLM requests. Before promotion they report the
empty case bank and calibration corpus as explicit blockers. `run` remains
strict and cannot start the solver until source, builder provenance, splits,
three model identities, and judge calibration are complete.
