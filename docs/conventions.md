# Framework and Experiment Conventions

## Ownership

- `src/ms_agent_eval` owns generic DSPy programs, source locking, case
  provenance, LLM judging, optimization governance, isolation, and reporting.
- `workspace.yaml` owns a target repository URL/ref, optional case-authoring
  reference repositories, exact instruction selection, two runtime role models, case
  location, judge calibration, and experiments.
- `case-authoring.md` owns the durable policy used to generate evaluation cases;
  `workspace.yaml` points to it explicitly.
- `cases/coverage.yaml` owns the complete skill-to-capability decomposition and
  minimum number of primary cases required for every capability.
- `cases/README.md` explains that general coverage contract inside every newly
  generated experiment workspace.
- Promoted case packages and human-labelled calibration inputs are versioned.
- External storage owns repository snapshots, solver
  calls, judge calls, compiled state, evaluations, and reports.

An evaluated repository must never become a core dependency or default path.

## Repository identity

- Author an HTTPS GitHub URL plus a tag or full commit.
- Resolve tags to a full 40-character commit before model execution.
- Select global instruction files explicitly.
- Select skills with exactly one of `directory` or `files`.
- Reject links, traversal, duplicate ids, missing files, empty discovery, and
  source/snapshot hash mismatches.
- Counts and file hashes are generated lock evidence, not user assertions.
- Resolve every authoring reference repository independently to a tag or full
  commit and store its snapshot outside Git.
- Expose selected reference evidence only as
  `references/<source-id>/<repository-path>`; target paths remain unqualified.
- Bind case provenance to the content-addressed authoring source set, not only
  the primary target snapshot.

## Case authoring and two runtime LLM roles

- The active Codex session authors cases through the repository-local
  `case-authoring` skill. It reads the exact versioned policy, locked target and
  reference snapshots, the user's campaign request, and existing case packages.
  It is not configured as an API model and never sees solver or judge evidence.
- `solver` and `judge` must resolve to distinct provider/model identities.
- The solver sees global context, skill context, and task only.
- The judge sees the rubric, expected response/artifacts, candidate response,
  task, and skill context.
- Every runtime LLM call records its role, rendered request, response, usage, latency,
  provider/model, parameters, and artifact identities externally.

## Cases and semantic judging

A promoted case contains:

```text
case.yaml
prompt.md
expected/response.md
expected/artifacts/       optional
rubric.yaml
```

Schema-v4 `case.yaml` declares id, title, skill, leakage group, exactly one
primary capability, at most two supporting capabilities, archetype, difficulty,
local file references, immutable source paths, and authoring provenance hashes. Direct edits
that change package identity invalidate provenance.

One Codex-authored package is one atomic case so it can be validated and
reviewed independently. It is not adequate coverage for
a skill. Every requested count is a lower bound and every selected skill gets at
least three materially distinct cases. `evaluation.cases.coverage` defines the
catalog plus minimum promoted cases and distinct leakage groups required per
skill. Scored-run preflight blocks until the catalog is complete, every
capability has its required primary-case depth, and both numeric thresholds are
met. Supporting tags do not satisfy primary coverage.

See [Capability coverage](capability-coverage.md) for the authoritative field
definitions, decomposition workflow, readiness formula, and change rules.

The authoring-instructions and capability-catalog hashes cover the exact policy
and semantic coverage contract bytes. Changing either makes earlier cases
incompatible with the active experiment until they are reviewed and
reclassified or re-authored under the new contract.

Diagnostic cases must reward diagnosis, not blind compliance. If the presented
evidence contains a real faulty test, false premise, broken configuration,
contradiction, or misleading symptom, the rubric includes
`error_identification` at 0.35–0.50 weight. Full credit requires locating the
actual defect and connecting it to the observed behavior; partial credit may be
given for recognizing the symptom or questioning the premise. Ordinary cases
without a grounded error do not include this criterion. All weights still sum
to exactly 1.0.

The configured DSPy judge LLM is the only semantic scorer. There are no Python
evaluator plugins, rule/checklist methods, evaluator status modes, keyword
judges, or manual-review scoring alternatives. Deterministic code may only:

- validate exact criterion and hard-failure ids;
- reject malformed scores outside `[0, 1]`;
- compute weighted totals;
- take the median criterion score across judge votes;
- apply majority voting to declared hard failures.

Judge calibration uses human-labelled strong, partial, incorrect,
contradictory, and adversarial candidate responses. Failure blocks every solver
call.

## Dataset governance

- Assign a leakage-resistant group, not each paraphrase, to a split.
- Codex assigns a semantic group; the configured deterministic policy maps it to a split.
- Optimization can load train and development only.
- Judge calibration for optimization may reference train/development cases,
  never test/challenge cases.
- Publish state-only DSPy JSON with pickle and unsafe LM state disabled.
- Load untouched test data only after candidate publication.
- Never use test results to revise cases, prompts, demonstrations, or optimizer
  parameters.
- Synthetic examples derived from one project example, defect, architecture
  slice, or likely solution share one leakage group across all paraphrases.
- Distillation/fine-tuning exports may use reviewed promoted train/development
  material only, remain outside Git, and retain source-set, authoring, case,
  split, and review provenance. Test/challenge prompts, answers, responses, and
  judge feedback never become training data.

## Runtime and secrets

- CPython 3.12+ is the baseline.
- `response_only` is valid when target code need not execute.
- Target code executes only in a digest-pinned Docker image with network none,
  a non-root user, read-only root filesystem, dropped capabilities, resource
  limits, bounded output, and automatic cleanup.
- Keep secrets in environment/provider facilities. Never commit them or include
  plaintext in locks and reports.

## Results

Every comparison locks repository commit/snapshot, case bytes and split,
authoring policy identity, solver program/model, judge program/model, calibration corpus,
runtime, and compiled artifact. Reports must not aggregate records across a
changed correctness contract without labeling the difference.
