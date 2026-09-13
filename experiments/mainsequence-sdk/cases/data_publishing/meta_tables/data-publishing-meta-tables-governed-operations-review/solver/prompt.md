# Goal
Review the proposed `eval-d1d604d8` design for this scenario: The team is working on a relational account, instrument, and position model graph. The requested slice is 'Use governed row and service operations' and the concrete outcome is to apply current row, query, and service APIs without bypassing ownership boundaries.

# Supplied evidence
- Required behavior: Apply current row, query, and service APIs without bypassing ownership boundaries.
- The current draft names `eval-d1d604d8` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: define stable logical and physical identities, explicit grain and keys, real foreign keys, deliberate cascades, rich column metadata, and migration-owned evolution.
- A success claim must be supported by the model graph, constraints, migration revision, governed operation result, and ownership boundary.
- Proposed implementation: The current proposal uses display names as identities, stores relationships as unrelated strings, omits composite uniqueness, and registers schemas at runtime.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
