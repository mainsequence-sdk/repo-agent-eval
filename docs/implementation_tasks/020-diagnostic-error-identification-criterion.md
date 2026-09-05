# 020 — Diagnostic Error-Identification Criterion

Status: Complete

## Problem

Some evaluation tasks contain the most important signal in the task itself: a
faulty test, incorrect premise, broken configuration, contradictory requirement,
or misleading failure symptom. A solver that blindly performs the requested
change can look productive while missing the actual problem. The rubric must
award substantial points to a solver that recognizes and explains the defect.

## Decision

For every case containing a real, grounded defect, the builder must add this
criterion:

```yaml
- id: error_identification
  weight: 0.4
  description: >
    Identifies the actual defect or root cause, connects it to the observed
    behavior, explains its consequence, and proposes the appropriate correction
    or next verification step.
```

The exact weight may vary from `0.35` through `0.50`, but it must be one of the
largest rubric criteria. All criterion weights continue to sum to exactly
`1.0`, so these are additional points within the normalized score rather than a
bonus beyond the maximum score.

## Scoring guidance

- Full credit: identifies the actual defect, cites the relevant evidence,
  explains the consequence, and gives a sound correction or verification step.
- Partial credit: notices the relevant symptom or challenges the faulty premise
  but does not locate the real root cause.
- No credit: overlooks the defect, invents an unrelated problem, or proceeds on
  the false premise without diagnosis.

The expected response must demonstrate the diagnosis. The solver-facing prompt
must contain enough observable evidence to make discovery fair without exposing
the answer or relying on builder-only context.

## Guardrail

The criterion is conditional. It must not appear in ordinary construction cases
that contain no real error, and the builder must never invent an unsupported
defect merely to add it. This prevents the judge from rewarding hallucinated
criticism.

## Architecture impact

No scorer or schema change is required. Rubrics already support arbitrary
weighted semantic criteria, and the LLM judge already scores every declared
criterion. The change belongs in the versioned case-authoring policy, which
means it changes the builder program hash and is automatically covered by the
existing provenance and stale-policy checks.

## Acceptance criteria

- Both the packaged default policy and MainSequence example policy contain the
  diagnostic-case instructions.
- The criterion id is exactly `error_identification`.
- Recommended weight is `0.35`–`0.50`.
- The policy specifies full, partial, and zero-credit behavior.
- The policy prohibits adding the criterion without a grounded error.
- Documentation explains that total rubric weight remains `1.0`.
- The packaged and example policies remain byte-identical.
- Ruff, tests, skill validation, and package build pass.

## Completion evidence

- Packaged and MainSequence policies are byte-identical.
- Ruff passed and all 42 tests passed.
- The case-authoring skill passed validation.
- Lock verification and package build passed.
- A wheel-level smoke test confirmed that the installed template contains the
  criterion and its weight guidance.
