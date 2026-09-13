# Goal
Review the proposed `eval-8b944ece` design for this scenario: The team is working on a Python 3.12 connector repository receiving an SDK and managed-skill refresh. The requested slice is 'Synchronize canonical sources' and the concrete outcome is to keep generated or copied managed content aligned with its canonical source and recorded pin.

# Supplied evidence
- Required behavior: Keep generated or copied managed content aligned with its canonical source and recorded pin.
- The current draft names `eval-8b944ece` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: freeze Git context, protect credentials, update dependency and lock state, refresh managed content from its canonical source, recover partial work, and report validation precisely.
- A success claim must be supported by branch and remote identity, dependency and lock diff, managed-source pin, focused tests, remaining failures, and exact changed state.
- Proposed implementation: The current proposal runs a blind global upgrade, edits copied managed skills by hand, discards unrelated changes, and calls a successful install proof of application behavior.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
