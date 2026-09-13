# Goal
Review the proposed `eval-726ab8ed` design for this scenario: The team is working on a relational account, instrument, and position model graph. The requested slice is 'Define the SQLAlchemy source contract' and the concrete outcome is to model the current declarative source and supported type behavior correctly.

# Supplied evidence
- Required behavior: Model the current declarative source and supported type behavior correctly.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
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
