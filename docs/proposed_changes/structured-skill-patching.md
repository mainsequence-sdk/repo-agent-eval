# Proposed Change: Structured Skill Patching

Status: **proposed; not implemented**

## Problem

The current COPRO skill target asks the optimizer to return a complete
replacement for a skill. That makes the candidate easy to compile and hash,
but it also gives the optimizer a large destructive surface: a useful local
change can accidentally remove unrelated instructions, examples, safety rules,
or source-grounded details.

The intended future behavior is for the optimizer to propose **structured
patches or explicit section replacements**, not an unconstrained whole-skill
rewrite.

## Candidate contract

A future proposal should identify the immutable base skill and describe a
bounded edit, for example:

```yaml
base_skill_hash: sha256:...
operations:
  - operation: replace_section
    section_id: error-handling
    expected_section_hash: sha256:...
    replacement_markdown: |
      ## Error handling
      ...
```

Possible operations are `replace_section`, `insert_after`, and `delete_section`.
Free-form unified diffs should not be the canonical model output because fuzzy
patch application would make validation and hashing ambiguous.

The compiled candidate must contain both the normalized operations and the
fully materialized skill text. Evaluation and Merkle identity use the
materialized text; provenance retains the patch, base hash, and application
result.

## Heuristics that must exist before implementation

The framework does not yet have safe answers for:

- stable section identity when headings are duplicated, renamed, or nested;
- preservation of front matter, required sections, examples, warnings, and
  repository-grounded facts outside the changed section;
- detection of semantic deletions hidden inside a replacement;
- maximum changed-section count and maximum changed-text ratio;
- conflict behavior when an earlier optimization changed the same base section;
- dependency-aware changes spanning multiple sections;
- deterministic normalization of Markdown and generated section identifiers;
- rejection, rollback, and diagnostic output for a patch that cannot apply
  exactly; and
- whether a whole-skill rewrite is ever allowed as an explicit, separately
  configured fallback.

Until those rules are specified and tested, structured patching must not be
enabled. A weak heuristic would only disguise whole-skill replacement as a
patch and could silently damage a skill.

## Required acceptance criteria

Implementation may begin only when it can demonstrate that:

1. the patch applies only to the declared base hash and exact section hashes;
2. application is deterministic and produces one canonical materialized skill;
3. untouched sections are byte-for-byte or canonically unchanged;
4. required content and source-grounded constraints are preserved or the
   proposal is rejected;
5. conflicting operations fail closed with an auditable reason;
6. patch, materialized output, validation findings, and lineage are all
   content-addressed; and
7. whole-skill replacement remains disabled unless a future ADR explicitly
   defines a safe fallback.

## Current behavior

COPRO continues to evaluate complete skill-overlay candidates. This document
does not change workspace schemas, optimizer prompts, compiled artifacts, or
runtime behavior.
