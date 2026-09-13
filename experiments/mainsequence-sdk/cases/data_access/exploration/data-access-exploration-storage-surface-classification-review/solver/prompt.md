# Goal
Review the proposed `eval-450e6306` design for this scenario: The team is working on an analyst looking for account positions and minute bars without known resource uids. The requested slice is 'Classify storage surfaces' and the concrete outcome is to distinguish row-oriented MetaTables from time-indexed data before choosing discovery operations.

# Supplied evidence
- Required behavior: Distinguish row-oriented MetaTables from time-indexed data before choosing discovery operations.
- Asset A is current through 10:00 UTC, asset B through 09:41 UTC, and a bounded correction starts at 09:55 UTC for A only.
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
