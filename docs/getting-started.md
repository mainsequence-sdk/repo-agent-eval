# Getting Started

This is the shortest complete workflow: select a repository and its skills,
build cases with one LLM, solve them with a second LLM, and score them with a
third LLM.

## 1. Install Python 3.12+

```bash
uv sync --python 3.12
```

DSPy is installed with the base library; no extra is needed.

## 2. Create one workspace manifest

For normal use, launch the guided setup. It resolves the repository first and
then lets you select global instructions and either a complete skills folder or
an exact list of `SKILL.md` files. It also configures the three models, runtime,
repetitions, initial case coverage, and optional optimization experiment:

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval setup
```

For automation, provide every choice explicitly:

```bash
mkdir my-evaluation
cd my-evaluation

uv run --project /path/to/ms-agent-eval ms-agent-eval init \
  --id example-evaluation \
  --repo https://github.com/example/project \
  --ref v1.2.3 \
  --global-instructions AGENTS.md \
  --skills-directory .agents/skills \
  --cases cases \
  --api-base-url http://127.0.0.1:8000/v1 \
  --case-builder-model builder-model \
  --solver-model solver-model \
  --judge-model judge-model
```

Use repeated `--skill-file path/to/SKILL.md` arguments instead of
`--skills-directory` for an exact list. The two forms are mutually exclusive.

`init` creates a schema-v4 `workspace.yaml`, `case-authoring.md`, an empty case
directory, a grouped split file, and a judge-calibration manifest. It does not
clone the target into the workspace. An `.env.example` is created only when
`--api-key-env` selects an external credential; a new workspace directory also
gets a `.gitignore` entry for the local `.env` secret file.

## 3. Configure three distinct models

The `init` command writes each model name and the shared API base URL directly to
`workspace.yaml`. Review or edit those versioned values there. The API location may
be shared, but each role must have a different provider/model identity.

If the API requires a credential, pass `--api-key-env OPENAI_API_KEY` during
initialization, copy `.env.example` to `.env`, and set only that secret:

```dotenv
OPENAI_API_KEY=your-secret
```

Model selection never comes from environment variables. `.env` is local and ignored.
If `workspace.data_root` is absent, all
generated data goes to `~/ms_agent_eval/example-evaluation`.

## 4. Build and promote cases

First review `case-authoring.md`. It is the durable experiment policy for how
the builder must ground tasks, avoid leakage, write reference answers, and
construct rubrics for the LLM judge. Its path is explicit configuration:

```yaml
evaluation:
  case_builder:
    instructions: case-authoring.md
```

The file's exact content becomes the DSPy case-builder signature instructions
and therefore changes the builder program hash. The `--coverage` value is not a
second policy file: it is the narrow, invocation-specific request supplied under
that durable policy. Changing `case-authoring.md` intentionally invalidates old
builder provenance, so affected cases must be rebuilt and explicitly promoted.

When a case includes a grounded faulty test, false premise, broken
configuration, contradiction, or misleading symptom, the generated rubric must
include `error_identification`. It normally carries 35–50% of the normalized
score, rewarding the solver for identifying the actual defect, explaining the
evidence and consequence, and proposing a correction. The criterion is omitted
when the case contains no real error.

The first validation of a new workspace exits successfully after validating the
manifest and immutable source, and reports `status: incomplete` plus exact
readiness blockers. It does not create an experiment lock or make a model call.
Build external drafts:

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval cases build \
  --workspace workspace.yaml \
  --coverage "Create grounded cases covering every discovered skill"
```

This resolves the repository tag to a commit, creates/reuses an immutable
external snapshot, loads `case-authoring.md`, invokes the configured DSPy case
builder once per selected skill, validates its typed output, and stores call
evidence plus drafts externally. It does not modify `cases/`.

Inspect and promote accepted drafts:

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval cases inspect-drafts \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval cases promote \
  --workspace workspace.yaml \
  --draft DRAFT_ID
```

Promotion writes the case package and assigns its leakage group to exactly one
split. The package records builder model/program, snapshot, request, and content
hashes; a direct edit invalidates provenance.

## 5. Add human-labelled judge calibration

`judge-calibration/manifest.yaml` must contain candidate responses labelled at
least `strong`, `partial`, `incorrect`, `contradictory`, and `adversarial`.
Every fixture references a promoted case and declares its human-accepted score
range. The LLM judge must pass this corpus before any solver request is made.

```yaml
schema_version: 2
fixtures:
  - id: strong-example
    case: case-id
    candidate: strong.md
    label: strong
    score_range: [0.9, 1.0]
```

## 6. Validate and inspect

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval validate \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval inspect \
  --workspace workspace.yaml
```

`inspect` shows the resolved commit, snapshot hash, selected instruction files,
discovered skill ids, case-authoring path and builder hash, case/split coverage,
all three DSPy program and model identities, calibration identity, runtime,
projected calls, and immutable experiment lock hashes before a scored run.

## 7. Evaluate, then optimize separately

```bash
uv run --project /path/to/ms-agent-eval ms-agent-eval run baseline \
  --workspace workspace.yaml

uv run --project /path/to/ms-agent-eval ms-agent-eval run optimize-few-shot \
  --workspace workspace.yaml
```

The baseline solves and judges promoted cases. Optimization sees only train and
development cases, compiles state-only DSPy JSON, publishes that artifact, and
only then loads the untouched test split for final evaluation. Solver and judge
usage are budgeted and reported separately.

All run output paths printed by the commands are under the external data root.
