# Goal
Review the proposed `eval-91e97796` design for this scenario: The team is working on a read-only audit of a connector failure with repository, SDK, and platform evidence. The requested slice is 'Build an evidence record' and the concrete outcome is to tie each finding to concrete supplied observations and distinguish fact from hypothesis.

# Supplied evidence
- Required behavior: Tie each finding to concrete supplied observations and distinguish fact from hypothesis.
- One local failure and one upstream source excerpt are supplied; neither establishes live platform state or issue ownership by itself.
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
