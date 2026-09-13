# Goal
Review the proposed `eval-3d1c9c58` design for this scenario: The team is working on a read-only audit of a connector failure with repository, SDK, and platform evidence. The requested slice is 'Preserve read-only audit status' and the concrete outcome is to audit evidence without mutating the repository, platform, issue tracker, or environment.

# Supplied evidence
- Required behavior: Audit evidence without mutating the repository, platform, issue tracker, or environment.
- The current draft names `eval-3d1c9c58` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: separate observation from hypothesis, localize the failing ownership boundary, inspect authoritative upstream evidence, avoid duplicates, and propose the smallest verification.
- A success claim must be supported by reproducible observations, exact source locations, ownership classification, competing hypotheses, duplicate search, and bounded next action.
- Proposed implementation: The current proposal mutates the checkout, labels the SDK defective from a downstream stack trace alone, and files a duplicate issue without ownership evidence.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
