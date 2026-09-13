# Goal
Review the proposed `eval-dbb3d7fd` design for this scenario: The team is working on an analyst looking for account positions and minute bars without known resource uids. The requested slice is 'Hand discovery into implementation' and the concrete outcome is to produce an auditable handoff containing selected identities, assumptions, and next operations.

# Supplied evidence
- Required behavior: Produce an auditable handoff containing selected identities, assumptions, and next operations.
- The current draft names `eval-dbb3d7fd` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: classify MetaTable versus time-index storage first, discover semantically, narrow with supported filters and columns, inspect details, and preserve ambiguity.
- A success claim must be supported by the observed catalog candidates, exact selected identity and contract, rejected alternatives, and unresolved facts.
- Proposed implementation: The current proposal guesses a uid and starts implementation after a name match without checking storage surface, grain, columns, or namespace.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
