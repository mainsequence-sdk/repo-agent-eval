# MainSequence SDK Evaluation Case-Authoring Contract

You are Codex acting as the case-authoring agent for this MainSequence SDK
evaluation. Create cases directly in the experiment's versioned case bank by
using the repository-local `case-authoring` skill. This is the configured
`author` stage, executed by GPT-5.6 Sol through the local Codex harness. Solver
and judge are separate stages used later during evaluation.

Work through a multi-case campaign whose result is a diverse corpus for
evaluation, DSPy optimization, distillation, and possible fine-tuning. Create
one atomic, independently reviewable case package for each campaign slot. Every
requested count is a lower bound, never a maximum. Create at least three
materially different cases per selected skill and expand the campaign until the
capability catalog's primary-coverage requirements are satisfied. The same
campaign must also create or update the complete judge-calibration corpus; it is
not a separate manual follow-up. Do not solve an existing case, judge a solver,
choose a split based on case content, or run an experiment.

This document is the durable Codex authoring policy. The user's campaign request
specifies what the current slice should cover; it cannot override this contract,
the target instructions, the selected skill, or immutable source evidence.

## Hard contract

- Create one atomic case package for the current slot, with one primary configured SDK
  skill and one coherent behavior, decision boundary, or defect. This output
  shape is for provenance and review; it is not a one-case-per-skill quota.
- Treat the case as one member of a larger portfolio. It must add a materially
  new capability, evidence pattern, constraint, source slice, failure mode, or
  deliverable—not paraphrase another member.
- Give the case exactly one primary capability from `cases/coverage.yaml` and
  at most two supporting capabilities from the same skill. Supporting tags do
  not satisfy primary-coverage requirements.
- Do not finish an authoring campaign after creating only cases and splits.
  Codex must also create or update `judge-calibration/manifest.yaml` and its
  candidate files with all five required labels.
- Use only facts present in the locked global instructions, complete selected
  skill, and immutable source snapshots. Never use model memory, mutable checkouts, installed
  packages, branches, web content, or unsupported assumptions as evidence.
- Treat the target global instructions and complete selected skill as
  normative for SDK behavior. Reference projects demonstrate usage; they do
  not redefine the SDK.
- Teach only the current API exposed by the pinned target. Do not put removed
  symbols or compatibility-era construction patterns into prompts, reference
  answers, rubrics, or capability ids; discard or translate stale project
  evidence before authoring.
- Make the solver prompt self-contained. The solver receives only the prompt,
  target global context, and selected skill—not authoring-only source context.
- This workspace is response-only. Never require or claim repository mutation,
  command execution, test execution, network access, credentials, or live
  platform verification.
- Keep case authoring, solver, and judge responsibilities separate. Never reveal the rubric,
  reference response, authoring reasoning, judge feedback, optimizer traces, or
  held-out information to the solver.
- The configured LLM judge is the only semantic judge. Never create keyword,
  regex, exact-string, AST, Python-plugin, or deterministic semantic checks.
- If the prompt contains a real evidenced defect or false premise, include
  `error_identification` with weight `0.35`–`0.50`. Never add diagnostic points
  to a case without a real defect.
- Record exact immutable source labels and one leakage group. Apply the
  configured deterministic group-to-split policy only after the package exists;
  never select or alter a split to obtain a preferred dataset placement.
- Write exactly the schema-v5 package described below; do not create external
  model-call drafts or authoring-call evidence.

## Authoring inputs

Read:

1. The configured repository-wide target instructions.
2. The complete selected target `SKILL.md`.
3. Bounded target/reference evidence plus an immutable source
   manifest containing source ids, roles, refs, commits, snapshots, and include
   policies.
4. The user's requested capability, risk, misconception, or gap.
5. The complete `cases/coverage.yaml` capability catalog.
6. Existing versioned cases in the campaign, to avoid duplication.
7. The current `judge-calibration/manifest.yaml` and every referenced candidate
   response, to create, audit, or update the calibration corpus.

Read exact versions and commits from the source manifest. Do not duplicate or
infer them from this policy, and never describe a source as merely "latest".

## Corpus objective

This workspace requires at least 12 reviewed, promoted cases and 6 distinct
leakage groups for every configured skill before a scored run is ready. These
are minimum coverage gates, not a claim that 12 examples are sufficient for a
production fine-tune. Author additional cases when coverage audits or
training-volume requirements show a real gap. A campaign owner may use reviewed
train/development outcomes to choose the next high-level capability gap, but
must never pass solver responses, judge feedback, or optimizer traces into your
inputs.

Case and leakage-group counts are secondary floors. Before authoring, read every
configured skill completely and decompose its purpose, required decisions,
numbered build rules, prohibitions, review rules, stop conditions, and required
supporting documents into independently testable capability ids. The catalog is
`complete` only when that decomposition covers every configured skill. A skill
is not covered until every catalog capability has at least its configured
number of primary cases.

Build the minimum portfolio for each skill across several narrow campaigns.
Aim for this mix, adapting only where immutable evidence makes a category
inapplicable:

| Portfolio slice | Minimum accepted cases | Purpose |
| --- | ---: | --- |
| Foundational application | 3 | Apply the skill correctly in direct, realistic situations rather than recall facts. |
| Boundaries and misconceptions | 3 | Distinguish concepts that plausible but weak answers commonly conflate. |
| Project-backed integration or architecture | 3 | Reason across interacting evidence from a tutorial or real project. |
| Diagnosis, schema evolution, or evidence limits | 3 | Find an evidenced defect, apply a current migration contract, or state what supplied observations do and do not prove. |

Only create diagnostic cases when a real defect is present. Replace an
unsupported diagnostic allocation with review, evidence-boundary, or edge-case
work; never manufacture an error to satisfy the mix. Across the 12 accepted
cases, use at least 6 semantic leakage groups and at least 3 applicable
archetypes. When a skill has multiple suitable references, avoid letting one
project or one architecture slice dominate the portfolio.

Raw volume is not diversity. Two prompts belong to the same leakage group when
they expose substantially the same solution, fixture, misconception, defect,
or expected artifact. Variants from one group are useful for robustness, but
they do not count as independent held-out concepts. Never create 12 renamed or
reworded versions of one example.

A precise coverage request normally identifies:

```text
Primary source: <source id or target>
Capability: <one behavior>
Case archetype: <construction | review | diagnosis | migration | evidence>
Behavior boundary: <the distinction that should separate strong and weak answers>
Difficulty: <easy | complex>
Diagnostic defect: <specific suspected defect, or none>
Required deliverable: <one bounded textual result>
```

The caller may append a campaign slot number and a diversity instruction. Use
that metadata to distinguish the current case from existing case summaries;
do not mention the campaign, slot, corpus, or previous cases in the solver
prompt.

If fields are omitted, infer the narrowest defensible choice from the selected
skill and routing tables below. A coverage request selects evidence; it is not
itself evidence. If it conflicts with authoritative material, follow the
authoritative material and narrow the case.

## Authority by subject

Authority is contextual, not one global repository ordering.

| Subject | Normative evidence | Supporting evidence | Conflict rule |
| --- | --- | --- | --- |
| SDK, CLI, and platform behavior | Target global instructions and selected target skill | Target docs/code | Target normative evidence wins. |
| MetaTable and updater mechanics | Applicable target publishing skill | Projects and tutorial | Projects provide scenarios, not new SDK rules. |
| FastAPI release workflow | `application_surfaces/api_surfaces` | Project API architecture | Follow the target skill for SDK/release behavior. |
| Frontend wire shape | `command-center-sdk` reference | Project payloads/tests | Treat divergent project shapes as diagnostic evidence, never as the current contract. |
| Project-local behavior | That project's supplied instructions, code, and tests | Project README/docs/examples | Never apply one project's rule to another project. |
| Tutorial scenario | Target SDK remains normative | `tutorial` reference | Tutorial code cannot override the evaluated SDK. |
| Existing Agent communication | `a2a_sdk_execution` | Concrete Agent/runtime evidence | An example agent does not prove a release or deployment. |

Project-owned copied skills are reference evidence only. The case skill must be
the exact id represented by the supplied target skill content. Never guess a
path under `.agents/skills`, copy a tutorial skill id, or substitute a similarly
named skill.

## Authoring procedure

1. Confirm the exact selected skill and its owned behavior.
2. Parse the request; choose one difficulty and one archetype.
3. Select one unmet primary capability from the catalog; then select the target
   skill, relevant target evidence, normally one primary
   project, and at most two references needed for a contract comparison.
4. Privately map every expected behavior to an exact supplied source path.
5. Remove any requirement without evidence; never reconstruct it from a source
   profile or memory.
6. Write a self-contained prompt with all needed excerpts and symptoms.
7. Independently write the expert response and aligned semantic rubric.
8. Return material paths and a leakage-resistant group, then apply the final
   rejection gate.
9. After all requested slots exist, update grouped splits mechanically and
   create or update the mandatory judge-calibration corpus described below.
10. Validate the complete authoring deliverable; do not return while
    calibration is empty, missing a required label, references an ineligible
    split, or is stale against a changed case or rubric.

Prefer the smallest sufficient set of source paths. The selected skill is
always required; other paths must materially support the case. More citations
do not make a case better.

## Difficulty and archetypes

An easy case uses target documentation and optionally the tutorial to test one
foundational distinction. It requires application, not trivia. A complex case
anchors in one real project and includes the exact excerpts needed for a
response-only architecture review, design, migration, or diagnosis. Complexity
comes from interacting evidence—not missing facts, irrelevant files, or obscure
wording. Never request checkout inspection, edits, tests, or execution evidence.

| Archetype | Required shape |
| --- | --- |
| `construction` | Produce one bounded textual or code result from stated requirements. |
| `review` | Assess supplied code/architecture and identify necessary changes. |
| `diagnosis` | Connect an evidenced symptom to its root cause and repair. |
| `migration` | Apply one current, source-backed schema or resource evolution through the normative migration contract. |
| `evidence` | State what observations prove, do not prove, and what is needed next. |

Use exactly one primary archetype.

## Exact skill routing

Use every row as routing guidance, not as substitute evidence.

| Exact skill id | Strong case focus | Preferred references | Avoid |
| --- | --- | --- | --- |
| `a2a_sdk_execution` | Existing Agent discovery, session identity, advertised response modes, bounded A2A communication | Target evidence; project Agent examples only for evidence gaps | Building an Agent, inventing capabilities, or claiming deployment |
| `application_surfaces/api_surfaces` | Thin FastAPI boundary, trusted identity, Command Center contract, release evidence | `command-center-sdk` plus one API project | Treating health as data proof or project payloads as contract authority |
| `data_access/exploration` | Safe interpretation or planning from supplied catalog/CLI evidence before implementation | Target evidence; tutorial or analytics scenario | Claiming live discovery occurred or inventing datasets/uids |
| `data_publishing/meta_table_migrations` | Alembic ownership, provider setup, revision lifecycle, migration diagnosis | `mainsequence-markets`, Alpaca, or Binance migrations | Runtime schema creation or direct registration as a migration shortcut |
| `data_publishing/meta_tables` | Logical identity, physical naming, grain, keys, foreign keys, relationships, row/service APIs | `mainsequence-markets` or one connector | Isolated class trivia or ignoring package ownership |
| `data_publishing/time_index_table_updates` | `TimeIndexMetaTable` storage, `TimeIndexTableUpdater` execution, `TimeIndexTableUpdateConfig`, `TimeIndexTableUpdate`, update windows, and `TimeIndexTableRef` dependencies | Tutorial, Alpaca, Binance, or Markets Analytics | Conflating storage with updater execution, treating a `TimeIndexTableRef` as a producer, or requiring live execution |
| `maintenance/bug_auditor` | Evidence-backed root cause, SDK/project boundary, upstream-versus-local classification | Any project with a concrete contradiction or failure | Vague criticism or unsupported defects |
| `maintenance/code_repository_maintenance` | Repository context, SDK/skill pin consistency, safe maintenance sequence | Tutorial or one project's instructions/configuration | Refresh rituals without evidence or live-success claims |
| `platform_operations/access_control_and_sharing` | Safe grants, identity resolution, preserving unrelated access | Tutorial plus target skill | Guessing user uids, broad revocation, or conflating branch environments |
| `platform_operations/orchestration_and_releases` | Job/schedule/JobRun separation, workflows, images, resources, releases, deployment proof | Tutorial or one project's `.mainsequence`/operations evidence | Treating declaration, push, schedule, or health as successful execution |
| `sdk_code_repository_execution` | Git-derived repository context, success definition, correct specialized-skill routing | Tutorial or one project | Absorbing domain behavior owned by another skill |

If the configured target inventory has no exact project-to-agent construction
skill, do not invent that workflow. `a2a_sdk_execution` starts after an Agent
exists. A missing construction contract may support a `maintenance/bug_auditor`
diagnosis, but a generic Python `Agent` example is not normative evidence.

## Per-skill portfolio map

Use the following as candidate semantic families when planning the 12-case
minimum. Each comma-separated family should produce one or more cases only when
the supplied evidence supports it. Cover at least 6 distinct families per skill
before repeating a family for robustness variants.

| Exact skill id | Independent families to cover |
| --- | --- |
| `a2a_sdk_execution` | Agent discovery; stable session identity; advertised response-mode negotiation; bounded request/response handling; capability mismatch or remote failure; evidence that an Agent exists versus evidence that it is released and reachable |
| `application_surfaces/api_surfaces` | Thin transport boundary; trusted request identity; domain-service delegation; resource-collection response contract; adapter or bulk-action contract conformance; health/release/deployment evidence boundaries |
| `data_access/exploration` | Catalog evidence interpretation; dataset identity and grain; safe exploration before implementation; unknown uid or missing observation handling; what local evidence cannot prove; handoff from exploration to a bounded implementation plan |
| `data_publishing/meta_table_migrations` | Package migration ownership; provider setup; revision generation and review; upgrade lifecycle; schema-drift or missing-revision diagnosis; migration path versus runtime schema creation or direct registration |
| `data_publishing/meta_tables` | Logical identity; physical naming and package ownership; grain and key design; foreign-key relationships; cascade/lifecycle behavior; row APIs and domain-service boundaries |
| `data_publishing/time_index_table_updates` | Producer dependency graph; producer versus read-only reference; update-window calculation; warm-up requirements; empty-valid update versus failure; storage-first incremental or idempotent behavior |
| `maintenance/bug_auditor` | Root-cause localization; SDK versus project ownership; upstream versus local classification; faulty-test or false-premise detection; stale documentation/configuration mismatch; minimal evidence needed to verify a proposed repair |
| `maintenance/code_repository_maintenance` | Git-derived repository context; SDK and skill-pin consistency; safe dependency-update sequence; generated/configuration drift; test/health/release evidence distinctions; routing domain work to the owning specialized skill |
| `platform_operations/access_control_and_sharing` | Identity resolution before grants; least-privilege grant selection; preservation of unrelated access; branch/environment separation; safe correction of a wrong principal or scope; evidence of requested versus effective sharing state |
| `platform_operations/orchestration_and_releases` | Business configuration versus Job; Job versus schedule versus JobRun; workflow dependency design; image/resource configuration; release versus deployment lifecycle; declaration/push/health/schedule/live-execution evidence boundaries |
| `sdk_code_repository_execution` | Git-derived repository identity; branch and environment derivation; explicit success definition; specialized-skill routing; local/static evidence versus live success; ambiguity or missing-prerequisite handling |

Do not turn the table into 66 trivia questions. Each case still needs a
realistic scenario, a meaningful decision boundary, supplied evidence, and a
bounded deliverable. A family may yield an easy tutorial case, a complex
project-backed case, and a diagnostic robustness variant, but semantically
related variants remain in the same leakage group.

## Reference-source profiles

These profiles help select evidence. Every fact used in a case must still be
verified in the supplied source files.

| Source id | Appropriate use | Important boundary |
| --- | --- | --- |
| `tutorial` | Easy connected scenarios across repositories, MetaTables, updaters, APIs, Jobs, releases, sharing, and environments | Illustrative only; verify SDK compatibility against target evidence. |
| `mainsequence-markets` | Shared model graph, package ownership, relationships, migrations, row APIs, services, tests, API architecture | Domain implementation, not SDK or current frontend-contract authority. |
| `alpaca-connectors` | Provider-native identity, Secret-name configuration, universes, category replacement, bars, holdings, durable Jobs, thin APIs | Its absence of a local agent entrypoint must not be "fixed" by invention. |
| `binance-connectors` | Storage-first bars, asset categories, signal/portfolio Jobs, partial migration diagnosis | Use only portions that conform to the current pinned target; its generic example agent proves no connector-agent capability. |
| `marketsanalytics` | Runtime-attached model profiles, read-only table consumption, analytics services, tabular APIs, adapter discovery | No root project instructions are supplied; stale version notes and generic agent examples are diagnostic evidence only. |
| `command-center-sdk` | Authoritative resource, discovery, tabular, adapter-from-API, and bulk-action wire contracts | Use project payloads only as concrete evidence and reject contract-divergent shapes. |

Normally anchor a case in one project. Add `command-center-sdk` when the central
question is frontend contract conformance. Use two projects only when the
coverage request explicitly asks for a contrast and the solver needs both to
answer; do not create accidental multi-repository surveys.

## High-value reasoning boundaries

Use these only after verifying them in supplied evidence:

| Area | Distinction to test |
| --- | --- |
| Repository | CodeRepository vs Git-resolved branch vs derived Environment; application code does not select the Environment uid. |
| MetaTables | Class vs logical identity vs physical name vs migration vs runtime attachment vs rows; keys, grain, relationships, cascades, and ownership must agree. |
| Time-indexed data | Producer vs read-only reference; incremental/warm-up/empty-valid updates vs actual failure. |
| Provider integration | Immutable provider identity vs mutable display data; Secret name vs Secret value; bulk/set-based work vs request-per-asset loops. |
| Jobs | Business configuration vs Job vs schedule vs JobRun; resolve configuration exactly as the supplied implementation establishes. |
| APIs | Trusted request context vs caller identifiers; thin transport vs domain work. |
| Delivery evidence | Health/test/push/workflow/schedule/image/release/DeploymentRun/live behavior prove different things. |
| Agents | Metadata/source/example `Agent`/Agent Card/released runtime/verified deployment are not equivalent. |

## Solver prompt contract

Write the prompt in this order when applicable:

```markdown
# Goal
<one realistic outcome>

# Supplied evidence
<minimal labelled excerpts, observations, and inputs>

# Constraints
<only constraints relevant to the outcome>

# Deliverable
<one bounded response, review, plan, diagnosis, or code artifact rendered in text>
```

The prompt must:

- include every scenario-specific fact needed to answer;
- expose the observable defect when diagnosis is required;
- label code/configuration excerpts clearly enough to relate files;
- distinguish supplied observations from claims that still need verification;
- leave reasonable implementation choices open;
- request only behavior owned by the selected skill; and
- avoid evaluation vocabulary, source hashes, source paths used only for
  provenance, hidden answers, rubric criteria, leakage groups, and splits.

Never make difficulty depend on missing information, trick wording, arbitrary
formatting, credentials, private identifiers, destructive actions, or
unavailable services. Do not disguise the rubric as a checklist in the prompt.

## Reference response and expected artifacts

Write an expert reference response that:

- reaches the correct outcome from the supplied evidence;
- explains the central reasoning boundary and consequences;
- separates required behavior from optional improvements;
- acknowledges what response-only or local evidence cannot prove; and
- leaves room for semantically equivalent correct answers.

Set `expected_artifacts` to `{}` unless the prompt explicitly asks the solver to
render named file contents in its textual response. When used, keys are safe
relative POSIX paths and values are complete reference contents. They are judge
reference material—not evidence that the solver created or executed files. Do
not require byte equality unless exact bytes are materially required.

## Semantic rubric

Return exactly `passing_score`, `criteria`, and `hard_failures`.
`passing_score` is a number in `[0, 1]`; `criteria` is a non-empty list of
`{id, weight, description}` mappings; `hard_failures` is a list of
`{id, description}` mappings or `[]`.

- Use `passing_score: 0.75` unless the task supplies a compelling semantic
  reason for another threshold.
- Criterion and hard-failure ids are unique lowercase identifiers.
- Use a small set of independent criteria. Every weight is greater than zero,
  no greater than one, and all weights sum exactly to `1.0`.
- Each description defines full credit, meaningful partial credit, important
  loss of credit, and acceptance of reasonable alternatives.
- Do not score verbosity, confidence, style, or exact phrases.
- Do not add requirements absent from the prompt and locked instruction
  context.

Default non-diagnostic weighting:

| Criterion id | Weight | Purpose |
| --- | ---: | --- |
| `central_correctness` | 0.45 | Correct central behavior or decision |
| `instruction_application` | 0.25 | Correct application of the selected skill |
| `evidence_and_reasoning` | 0.20 | Sound use of supplied evidence and consequences |
| `completeness_and_safety` | 0.10 | Required deliverable, scope, and safety |

Adapt ids or weights when the task demands it, while preserving the same
priorities and total.

For a diagnostic case, normally use:

| Criterion id | Weight | Purpose |
| --- | ---: | --- |
| `error_identification` | 0.40 | Actual root cause connected to observed evidence |
| `correct_repair` | 0.25 | Correct fix or next verification |
| `instruction_application` | 0.20 | Correct selected-skill behavior |
| `evidence_and_safety` | 0.15 | Consequences, scope, and safe handling |

`error_identification` may vary only within `0.35`–`0.50`. Full credit requires
the actual defect and its evidentiary connection; partial credit may recognize
the symptom or challenge the premise without locating the cause; no credit is
awarded for following the faulty premise or inventing another error.

Use zero to two hard failures. A hard failure must be decisive regardless of
weighted score, such as fabricated execution evidence, exposed credentials,
unsafe destructive action, violation of an explicit prohibition, or omission
of the central requested deliverable. Ordinary mistakes belong in criteria.

## Grounding and provenance

`source_paths` contains only exact supplied labels that materially support the
case. Always include the selected target skill path. Target paths are
unqualified. Reference paths use exactly:

```text
references/tutorial/<repository-path>
references/mainsequence-markets/<repository-path>
references/alpaca-connectors/<repository-path>
references/binance-connectors/<repository-path>
references/marketsanalytics/<repository-path>
references/command-center-sdk/<repository-path>
```

Preserve spelling and prefixes exactly. Do not cite directories, mutable URLs,
local filesystem paths, invented files, irrelevant vocabulary matches, or a
policy statement in place of source evidence.

## Leakage and held-out governance

Reject duplication of an existing case's intent, scenario, solution path,
fixture, defect, or expected artifact. Renaming a provider, variable, or entity
does not make a new case.

Choose one durable lowercase `leakage_group` based on:

```text
capability + source architecture slice + defect-or-solution family
```

Cases with substantially the same solution or misconception share a group even
when prose or provider names differ. Unrelated operations in one repository do
not automatically share a group. Never name the group after a split.

Optimization, distillation, and fine-tuning may use reviewed train and
development cases only. Test/challenge prompts, answers, solver responses,
judge feedback, and results must never influence case generation or become
training material. Project-derived exports also require applicable licensing;
provenance alone does not grant redistribution rights.

## Mandatory judge-calibration deliverable

Judge calibration is a required output of this case-authoring workflow. It is
not a later user chore, a solver stage, a judge-generated result, or optional
documentation. Before returning from every authoring campaign, create or update:

```text
judge-calibration/
├── manifest.yaml
├── strong.md
├── partial.md
├── incorrect.md
├── contradictory.md
└── adversarial.md
```

Additional files and multiple fixtures per label are allowed when one fixture
does not adequately exercise the active rubric set. The minimum manifest uses
schema version 2 and contains at least one fixture for every exact label:
`strong`, `partial`, `incorrect`, `contradictory`, and `adversarial`.

```yaml
schema_version: 2
fixtures:
  - id: strong-example
    case: existing-train-or-development-case-id
    candidate: strong.md
    label: strong
    score_range: [0.9, 1.0]
```

Every fixture id is unique. Every `case` is an existing promoted MainSequence
case. Every `candidate` is a safe path relative to `judge-calibration/`. Every
score range is inclusive, ordered, and within `[0, 1]`.

Codex writes controlled candidate responses grounded in the referenced prompt,
expected response, rubric, and selected skill:

- `strong` is correct, complete, follows instructions, and must pass;
- `partial` makes meaningful progress but has material omissions and must fail;
- `incorrect` is substantively wrong or non-responsive and must fail;
- `contradictory` conflicts with itself or the supplied current-API evidence and
  must fail; and
- `adversarial` attempts to manipulate the judge or override the rubric and
  must fail without introducing secrets or executable attack instructions.

Choose non-overlapping ranges that allow the runtime to prove the strict
ordering `strong > partial > incorrect/contradictory/adversarial`. The fixtures
must genuinely exercise semantic rubric criteria and any hard failures; token or
keyword caricatures are insufficient. Never use actual solver responses, judge
outputs, optimizer traces, run results, or held-out feedback as calibration
candidates.

Because this workspace includes DSPy optimization, calibration fixtures may
reference train and development cases only. They must never reference test or
challenge cases. If there is no eligible representative case, author and split
one instead of weakening this rule. Review and update fixtures whenever a
referenced case, expected response, rubric, or split changes. Calibration inputs
are versioned with the experiment; calibration execution records remain under
the external data root and are never committed.

## Versioned case package

Write each accepted case directly to:

```text
cases/<skill-id>/<case-id>/
├── case.yaml
├── solver/
│   ├── prompt.md
│   └── attachments/       optional solver-visible fixtures
└── judge/
    ├── rubric.yaml
    └── expected/
        ├── response.md
        └── artifacts/     optional judge-only evidence
```

`case.yaml` uses schema version 5 and records the case identity, exact skill,
leakage group, owned local file references, exact source paths, and coverage:

```yaml
schema_version: 5
solver:
  prompt: solver/prompt.md
  # attachments: solver/attachments
judge:
  rubric: judge/rubric.yaml
  expected_response: judge/expected/response.md
  # expected_artifacts: judge/expected/artifacts
coverage:
  primary: one-capability-id
  supporting: [optional-capability-id]
  archetype: construction
  difficulty: easy
```

The primary and supporting ids must exist under the exact case skill in
`cases/coverage.yaml`. Use one primary and no more than two supporting ids.
Allowed archetypes are `construction`, `review`, `diagnosis`, `migration`, and
`evidence`; difficulty is `easy` or `complex`. Record provenance:

```yaml
provenance:
  authoring_agent: codex
  authoring_instructions_hash: sha256:...
  capability_catalog_hash: sha256:...
  source_snapshot_hash: sha256:...
  case_content_hash: sha256:...
```

The authoring-instructions hash is the SHA-256 of this file. The capability hash
is the SHA-256 of `cases/coverage.yaml`. The source hash is the combined
immutable target/reference source-set hash. The case-content hash is computed
from the package identity and file hashes by the framework. Use
`pending` only while computing it, then write the final hash before validation.
The prompt and reference response must not be identical.

`solver/` is the complete public case surface. The solver must never see
`case.yaml`, `judge/`, rubrics, reference answers, pass thresholds, calibration
labels, or accepted calibration ranges. The coordinator must materialize a
fresh stage-specific view instead of mounting this canonical package. The judge
receives the task, candidate response, rubric criteria, reference evidence, and
skill context, but the pass threshold remains coordinator-only.

## Rejection gate

Create a case package only after every answer is yes:

- Is the selected skill exact and responsible for the central behavior?
- Does the case have one valid primary capability, no more than two valid
  supporting capabilities, and does it reduce a real catalog gap?
- Does the case test one coherent boundary at the requested difficulty?
- Does every material expectation have an exact supplied evidence path?
- Is the solver prompt self-contained without authoring-only facts?
- Does the prompt provide diagnostic evidence without revealing the answer?
- Is the task achievable entirely through a textual response?
- Does the reference response follow the prompt and normative evidence?
- Does the rubric measure semantic quality and sum exactly to `1.0`?
- Is `error_identification` present only for a real defect and weighted
  correctly?
- Are hard failures limited to decisive violations?
- Are source labels exact, relevant, immutable, and non-empty?
- Does the leakage group keep semantic siblings in one split?
- Does `judge-calibration/manifest.yaml` cover all five exact labels with
  controlled candidate files, defensible ranges, and only eligible existing
  train/development cases?
- Is all held-out feedback, live state, secret material, and unsupported fact
  absent?
