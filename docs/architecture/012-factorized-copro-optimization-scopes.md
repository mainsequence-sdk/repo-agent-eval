# ADR-012: Factorized COPRO Optimization Scopes

- Status: Accepted
- Date: 2026-09-15
- Decision owners: MS Agent Eval maintainers
- Scope: COPRO search-space configuration, frozen instruction boundaries,
  skill-level optimization, global-instruction optimization, compiled-program
  identity, invalidation, validation, and reporting
- Supersedes: the joint prompt-and-skill target and alternating-search sections
  of [ADR-007](007-configurable-dspy-optimization-strategies.md)
- Refines:
  [ADR-003](003-content-addressed-experiment-graph.md),
  [ADR-008](008-paired-copro-evaluation-and-formal-reporting.md), and
  [ADR-011](011-stateful-optimization-and-semantic-reuse.md)

## Context

ADR-007 introduced COPRO with this configuration:

```yaml
optimizer:
  strategy: copro
  targets:
    prompt: true
    skills: all
```

That contract combines two different optimization questions in one search:

1. which shared instruction should govern the solver; and
2. which capability-specific instruction should govern each skill.

It also uses the word `prompt` for the DSPy signature instruction and response
field prefix. It does not mean the repository's configured `AGENTS.md` global
instruction source. A user asking to optimize `AGENTS.md` therefore cannot
express the intended experiment accurately.

Joint search creates a more serious experimental problem. If shared global
instructions are allowed to change while each skill is searched, the result
can become a collection of skill-local agents:

```text
skill A winner = global candidate A + skill candidate A
skill B winner = global candidate B + skill candidate B
skill C winner = global candidate C + skill candidate C
```

That is not one reusable agent with an optimized skill bank. It confounds the
effect of the global instructions with the effect of every skill and produces
an unnecessarily large search space. Conversely, an experiment intended to
optimize the global agent instructions must not silently rewrite skills.

The benchmark needs two explicit and mutually exclusive COPRO experiments:

- freeze global instructions and optimize skills; or
- freeze skills and optimize global instructions.

The fixed validation holdout and stateful round lifecycle from ADR-011
remain unchanged. This decision changes what is allowed to vary, not the
train/validation/test safety model.

## Decision

Every COPRO experiment declares exactly one optimization search space:

```text
                              COPRO experiment
                                     |
                              search_space.type
                                     |
                  +------------------+------------------+
                  |                                     |
                skills                        global_instructions
                  |                                     |
       global instructions frozen                 skills frozen
       DSPy signature frozen                      DSPy signature frozen
       selected skills may vary              global instruction bundle may vary
                  |                                     |
         one overlay per skill                one global winner for all skills
                  +------------------+------------------+
                                     |
                          frozen compiled program
                                     |
                        paired untouched-test run
```

There is no joint mode. There is no implicit default. The configuration cannot
enable both search spaces in one experiment.

The base DSPy solver signature instruction, signature field definitions, and
response-field prefix remain frozen in both search spaces. This ADR removes
them from COPRO search. A future decision may introduce a distinct signature
optimization experiment, but it must not be conflated with `AGENTS.md` or
skill optimization.

## Search space A: optimize skills

```yaml
experiments:
  optimize-skills:
    mode: optimize
    based_on: baseline
    roles:
      solver: solver
      optimizer: optimizer
      judge: judge
    optimizer:
      strategy: copro
      search_space:
        type: skills
        include: all
      search:
        breadth: 3
        depth: 2
        init_temperature: 1.4
      metric:
        name: rubric_mean_score
    validation:
      strategy: holdout
      fraction: 0.2
      seed: 0
```

`include` accepts either `all` or a non-empty list of exact configured skill
identifiers:

```yaml
search_space:
  type: skills
  include:
    - data_publishing/meta_tables
    - platform/data_nodes
```

For this search space:

- every file in `evaluation.instructions.global` is immutable and identified
  by its locked content hash;
- the DSPy signature instruction and response prefix are immutable;
- every unselected skill remains immutable;
- each selected skill is an independent optimization target;
- a skill candidate is evaluated only with cases owned by that skill;
- one stateful candidate history is carried across that skill's search rounds;
- all candidates for that skill use the same fixed skill-specific validation
  evidence; and
- the winning overlays are assembled into one compiled skill bank after all
  selected skill searches finish.

Independent skill searches may execute concurrently within the configured
model and endpoint capacities. Concurrency does not change candidate identity
or selection semantics.

The compiled result is one agent configuration:

```text
frozen global instruction bundle
    + frozen DSPy signature
    + original skill text for every unselected skill
    + exactly one selected overlay for every optimized skill
```

It is not permissible to attach a different global-instruction candidate to
each skill winner.

## Search space B: optimize global instructions

```yaml
experiments:
  optimize-global-instructions:
    mode: optimize
    based_on: baseline
    roles:
      solver: solver
      optimizer: optimizer
      judge: judge
    optimizer:
      strategy: copro
      search_space:
        type: global_instructions
        files:
          - agent_scaffold/AGENTS.md
      search:
        breadth: 3
        depth: 2
        init_temperature: 1.4
      metric:
        name: rubric_mean_score
    validation:
      strategy: holdout
      fraction: 0.2
      seed: 0
```

`files` accepts either `all` or a non-empty list of exact paths already
declared by `evaluation.instructions.global`. Selected files form one ordered
global-instruction bundle and are optimized as one target. File order and
boundaries remain explicit in the compiled artifact. Unselected global files
remain frozen.

For this search space:

- every configured skill, including imported and namespaced skills, is
  immutable;
- the DSPy signature instruction and response prefix are immutable;
- the optimizer produces alternatives only for the selected global
  instruction bundle;
- every candidate is evaluated across a deterministic, stratified fitting and
  validation corpus covering the affected skills;
- candidate selection uses the configured metric over the complete global
  validation evidence, not a separate global winner per skill; and
- the final artifact contains one selected global bundle combined with the
  original locked skill bank.

This experiment answers whether a single improved `AGENTS.md`-level instruction
bundle helps the agent across skills. It cannot produce different global
instructions for different cases, skills, rounds, or test arms.

## Workspace validation

Workspace schema 17 replaces `optimizer.targets` with the discriminated
`optimizer.search_space` union. No compatibility shim is added to active
workspace parsing.

Validation rejects:

- an absent `search_space` for `strategy: copro`;
- the removed `targets` or `targets.prompt` fields;
- a search space containing both skill and global-instruction selectors;
- unknown search-space types;
- `all` combined with an explicit list;
- empty lists, duplicate entries, glob syntax, or unknown skill identifiers;
- global paths not present in `evaluation.instructions.global`;
- skill identifiers not present in the locked local/imported skill inventory;
- attempts to select the DSPy signature instruction or output prefix; and
- insufficient group-safe optimization-training or validation evidence for
  any selected target.

An immutable historical run retains its original frozen workspace definition
and remains inspectable. Resuming a historical run uses the frozen internal
representation required by its schema. A newly authored workspace must use the
new explicit search-space contract.

## Optimization and validation lifecycle

Both search spaces use ADR-011's fixed validation holdout and one stateful
search over the complete optimization-training set:

```text
permanent train
      |
      +--> fixed group-safe validation holdout
      |
      +--> complete optimization-training set
                    |
            stateful COPRO rounds
                    |
              selected winner
                    |
        materialize compiled program once
                    |
       paired original/optimized untouched test
```

For skill optimization, the state chain and winner are maintained separately
for each selected skill. For global-instruction optimization, there is one
state chain and one winner across the complete affected corpus.

Validation and test cases never become proposal inputs. The optimizer may
receive current candidate text, previous candidate text, numeric scores, and
non-sensitive aggregate search statistics. It must not receive expected
responses, rubrics, judge feedback, test content, or challenge content.

## Factorized content identity

The locked instruction program is represented by separate components:

```text
G0       = hash(ordered configured global instruction files)
S0[id]   = hash(source text and provenance for skill id)
G*[id]   = selected global bundle candidate, when global search is active
S*[id]   = selected skill overlay, when skill search is active
P        = hash(frozen DSPy signature and output contract)
```

The effective solver input for a case owned by skill `s` is:

```text
effective_global_hash
    + effective_skill_hash[s]
    + P
    + case solver view
    + solver model/deployment/parameters
    + replicate slot
```

The work identity must not contain inactive candidates or unrelated skill
overlays.

Consequently:

- changing or selecting an overlay for skill `A` invalidates solver work only
  for cases whose effective context uses skill `A`;
- unchanged skills and their responses remain reusable across skill-search
  experiments;
- changing the effective global bundle invalidates solver work for every case
  that receives that bundle;
- baseline responses are reusable by either experiment when their effective
  global and skill hashes match the baseline;
- round, candidate position, run id, and experiment id remain
  attempt/provenance fields rather than semantic work-key inputs; and
- identical effective model-visible requests reuse their validated semantic
  results regardless of which search produced them.

The compiled-program artifact records both the frozen and effective hashes. A
skill-search artifact contains no optimized global bundle. A
global-instruction-search artifact contains no skill overlays.

## Compiled artifacts

### Skill optimization artifact

The compiled artifact records:

- search-space type `skills`;
- frozen global-instruction bundle hash and source paths;
- frozen DSPy signature/program hash;
- original and selected content hashes for every optimized skill;
- unchanged source hashes for unselected skills;
- per-skill proposal and selection lineage;
- per-skill validation evidence and score; and
- final assembled skill-bank hash.

### Global-instruction optimization artifact

The compiled artifact records:

- search-space type `global_instructions`;
- selected global source paths and their original hashes;
- selected global bundle content, ordered source membership, and effective hash;
- frozen DSPy signature/program hash;
- complete frozen skill-bank inventory and hash;
- one global proposal and selection lineage; and
- whole-corpus validation evidence and score.

Neither artifact overwrites the target repository, experiment instruction
sources, imported skill snapshot, or authored case bank.

## Reporting and recorder projection

Every optimization report and recorder projection identifies the search-space
type before presenting scores.

Skill-optimization reporting includes:

- baseline and optimized untouched-test score per skill;
- signed delta per skill, ordered by delta and then stable skill id;
- original and selected skill hashes;
- validation score and selection lineage per skill;
- unchanged/unselected skills;
- assembled skill-bank hash; and
- aggregate paired baseline-versus-optimized test results.

Global-instruction reporting includes:

- original and selected global-bundle hashes;
- the exact selected global source paths;
- whole-corpus validation score and selection lineage;
- aggregate paired untouched-test delta;
- per-skill test deltas and regressions under the single global winner; and
- the frozen skill-bank hash proving that skills did not change.

Reports must not label a skill-search result as an optimized agent prompt or a
global-instruction result as an optimized skill. Cross-run comparisons are
valid only when benchmark revision, search-space type, solver/judge identities,
and reporting protocol are explicit.

## Consequences

- The two causal questions are separated and interpretable.
- `AGENTS.md` is frozen during skill optimization, eliminating skill-local
  variants of the global agent.
- Skills are frozen during global-instruction optimization, so measured lift
  can be attributed to the shared instruction bundle.
- Removing joint alternating search reduces candidate volume and execution
  time.
- Per-skill Merkle invalidation becomes narrow and reusable.
- Global-instruction changes correctly invalidate the broad solver surface
  they affect.
- Comparing interactions between optimized global instructions and optimized
  skills requires a later, explicitly designed composition experiment. It is
  not inferred by applying both optimizations in one COPRO run.
- Existing workspaces using `targets.prompt` or joint targets require an
  explicit migration to one of the two search spaces.

## Required invariants

1. Exactly one COPRO search-space type is active in an experiment.
2. Skill optimization cannot change global instructions, the DSPy signature,
   or the response prefix.
3. Global-instruction optimization cannot change any skill, the DSPy
   signature, or the response prefix.
4. One global-instruction experiment produces exactly one global winner, not
   one winner per skill.
5. One skill-search winner may exist per selected skill, but all winners share
   the same frozen global bundle.
6. Only content that changes the effective model-visible request may change
   semantic solver work identity.
7. Validation evidence selects candidates; untouched test evidence only
   measures the already frozen program.
8. Compiled artifacts prove which instruction components were frozen and which
   were optimized.
9. Historical frozen runs remain inspectable without weakening the active
   workspace schema.
