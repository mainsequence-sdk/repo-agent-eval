# Goal
Review the proposed `eval-66f14ef2` design for this scenario: The team is working on a read-only audit of a connector failure with repository, SDK, and platform evidence. The requested slice is 'Localize the root cause' and the concrete outcome is to connect an observable failure to the actual defective boundary rather than restating symptoms.

# Supplied evidence
- Required behavior: Connect an observable failure to the actual defective boundary rather than restating symptoms.
- The current draft names `eval-66f14ef2` but does not yet separate declaration, owned operation, and observable runtime result.
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
