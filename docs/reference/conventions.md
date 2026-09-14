# Framework and Experiment Conventions

## Ownership

- `src/ms_agent_eval` owns generic DSPy programs, source locking, case
  provenance, LLM judging, optimization governance, isolation, and reporting.
- `workspace.yaml` owns a target repository URL/ref, optional case-authoring
  reference repositories, exact instruction selection, stage executors and models,
  recorder projection, case location, judge validation, and experiments.
- `case-authoring.md` owns the durable policy used to generate evaluation cases;
  `workspace.yaml` points to it explicitly.
- `cases/coverage.yaml` owns the complete skill-to-capability decomposition and
  minimum number of primary cases required for every capability.
- `cases/README.md` explains that general coverage contract inside every newly
  generated experiment workspace.
- Reviewed case packages and reference-labelled judge-validation inputs
  are versioned.
- External storage owns repository snapshots, solver
  calls, judge calls, compiled state, evaluations, reports, and retryable recorder
  delivery state.
- An external recorder such as MLflow is a derived comparison and inspection
  surface. It is never authoritative for scores, resume, or cache reuse.

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

## Case authoring and model-driven stage roles

- The configured `author` stage authors cases through the repository-local
  `case-authoring` skill. For the MainSequence workspace this is a local Codex
  task using `gpt-5.6-sol`. It reads the exact versioned policy, locked target and
  reference snapshots, the user's campaign request, and existing case packages.
  It never sees solver or judge evidence.
  The same campaign owns the capability catalog, grouped splits, and complete
  judge-validation inputs; judge validation is not deferred to a manual follow-up.
- `solve` and `judge` are distinct stage roles; they may resolve to the same
  model and executor when explicitly configured.
- The solver sees global context, skill context, and task only.
- The judge sees the rubric, expected response/artifacts, candidate response,
  task, and skill context.
- Every runtime LLM call records its role, rendered request, response, usage, latency,
  provider/model, parameters, and artifact identities externally.

## Cases and semantic judging

A reviewed case contains:

```text
case.yaml
solver/prompt.md
solver/attachments/       optional solver-visible fixtures
judge/rubric.yaml
judge/expected/response.md
judge/expected/artifacts/ optional judge-only evidence
```

Schema-v6 `case.yaml` declares id, title, skill, leakage group, exactly one
primary capability, at most two supporting capabilities, archetype, local file
references, immutable source paths, and authoring provenance hashes. Direct edits
that change package identity invalidate provenance.

Cases do not carry subjective difficulty labels. Reports measure the exact
hidden expected-response token count and derive fixed response-length bands.

The coordinator never mounts this canonical package into a model runtime. It
projects `solver/` and `judge/` into independent, immutable stage views so a
solver cannot read judging material and a judge cannot read coordinator-only
fixture labels.

One Codex-authored package is one atomic case so it can be validated and
reviewed independently. It is not adequate coverage for
a skill. Every requested count is a lower bound and every selected skill gets at
least three materially distinct cases. `evaluation.cases.coverage` defines the
catalog plus minimum reviewed cases and distinct leakage groups required per
skill. Scored-run preflight blocks until the catalog is complete, every
capability has its required primary-case depth, and both minimum counts are met.
Supporting tags do not satisfy primary coverage.

See [Capability coverage](../evaluation/capability-coverage.md) for the authoritative field
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

Judge validation uses reviewed, reference-labelled strong, partial, incorrect,
contradictory, and adversarial candidate responses created with the case bank.
The candidates are controlled authoring inputs, never real solver or judge
outputs. Failure blocks every solver call.

## Dataset governance

- Assign a leakage-resistant group, not each paraphrase, to a split.
- Codex assigns a semantic group; the configured deterministic policy maps it to a split.
- Optimization derives grouped K-fold cross-validation within train.
- Judge validation may reference training cases,
  never test/challenge cases.
- Persist state-only DSPy JSON with pickle and unsafe LM state disabled.
- Select by mean out-of-fold score, refit on all training cases, and evaluate
  untouched test data once.
- Never use test results to revise cases, prompts, demonstrations, or optimizer
  parameters.
- Synthetic examples derived from one project example, defect, architecture
  slice, or likely solution share one leakage group across all paraphrases.
- Distillation/fine-tuning exports may use reviewed training
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
authoring policy identity, solver program/model, judge program/model, judge-validation set,
runtime, and compiled artifact. Reports must not aggregate records across a
changed correctness contract without labeling the difference.
