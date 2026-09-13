# Experiment Setup and Requirements

An experiment is a versioned definition of what repository behavior is being
tested and how model-driven stages should run. The small Git workspace contains
reviewable inputs; immutable source snapshots and every generated artifact live
under the external data root.

## Required setup

```mermaid
flowchart TB
    subgraph Source[Target definition]
        Repository[HTTPS repository URL]
        Revision[Tag or full commit]
        Global[Global instruction files]
        Skills[Skill directory or exact skill files]
    end

    subgraph Corpus[Evaluation definition]
        Policy[case-authoring.md]
        Catalog[cases/coverage.yaml]
        Cases[Promoted case packages]
        Splits[cases/splits.yaml]
        Fixtures[Judge-calibration fixtures]
    end

    subgraph Runtime[Execution definition]
        Executors[Executors]
        Models[Author, solver, and judge models]
        Stages[Stage bindings and budgets]
        Experiments[Baseline or optimization configuration]
    end

    Source --> Snapshot[Immutable source snapshots]
    Snapshot --> Authoring[Grounded case authoring]
    Policy --> Authoring
    Authoring --> Catalog
    Authoring --> Cases
    Cases --> Splits
    Cases --> Fixtures
    Runtime --> Lock[Content-addressed experiment lock]
    Catalog --> Ready{Readiness gate}
    Splits --> Ready
    Fixtures --> Ready
    Lock --> Ready
    Ready -->|Pass| Calibrate[Run judge calibration]
    Calibrate -->|Pass| Run[Solver stages allowed]
    Calibrate -->|Fail| Stop[Stop before solver calls]
```

The framework will not begin a scored run until each required identity and
integrity check can be locked.

| Requirement | Where it is declared | What must be true |
| --- | --- | --- |
| Target source | `evaluation.repository` | URL/ref resolves to one immutable commit and snapshot. |
| Instructions | `evaluation.instructions` | Global files exist; exactly one of `skills.directory` or `skills.files` is selected. |
| Authoring policy | `evaluation.case_authoring.instructions` | The versioned file exists and its exact bytes are included in provenance. |
| Capability catalog | `evaluation.cases.coverage.catalog` | Status is complete and every selected skill/capability is represented. |
| Case bank | `evaluation.cases.directory` | Packages validate, retain provenance, and meet capability depth and diversity floors. |
| Splits | `evaluation.splits` | Whole leakage groups map deterministically to train, validation, test, or challenge. |
| Judge calibration | `evaluation.judge.calibration` | Controlled fixtures exist and the configured judge passes their accepted score ranges. |
| Execution | `executors`, `models`, and `stages` | Every stage has a valid executor, model where required, and finite budget. |
| Run definition | `experiments` | The selected experiment is either an evaluation or an optimization based on an evaluation. |

## Versioned workspace layout

```text
<experiment>/
├── workspace.yaml
├── case-authoring.md
├── .env.example                 optional credential-name template
├── cases/
│   ├── README.md
│   ├── coverage.yaml
│   ├── splits.yaml
│   └── <skill>/<case>/
│       ├── case.yaml
│       ├── solver/
│       └── judge/
└── judge-calibration/
    ├── manifest.yaml
    └── *.md
```

Repository snapshots, locks, requests, responses, optimizer state, results,
and reports do not belong in this directory. See the
[architecture overview](../architecture/index.md) for the storage boundary.

## From empty workspace to benchmark

1. Run `ms-agent-eval setup` interactively or `ms-agent-eval init` for an
   explicit configuration.
2. Resolve the target and optional reference sources to immutable snapshots.
3. Ask the configured author stage to build the capability catalog, case bank,
   grouped splits, and calibration corpus.
4. Run `validate` and `inspect`. Address every reported blocker.
5. Use a pilot while the corpus is incomplete; treat its output only as
   diagnostics.
6. Run the baseline benchmark, an optimization experiment, or both after the
   scored-run readiness gate passes.
7. Compare only results whose locked evaluation identities are compatible.

Use the [getting-started guide](getting-started.md) for commands and a complete
configuration example. Use [target sources](target-sources.md) for snapshot and
reference-repository rules. The focused guides cover
[workspace configuration](workspace-configuration.md),
[case authoring](case-authoring.md), and
[running an experiment](running-an-experiment.md).
