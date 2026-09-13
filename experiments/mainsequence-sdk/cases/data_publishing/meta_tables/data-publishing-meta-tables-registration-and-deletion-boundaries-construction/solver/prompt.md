# Goal
Design the `eval-5dbf5632` implementation slice for this scenario: The team is working on a relational account, instrument, and position model graph. The requested slice is 'Respect registration and deletion boundaries' and the concrete outcome is to distinguish registration, resource lifecycle, and protected deletion responsibilities.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-5dbf5632-repo` and `eval-5dbf5632-env`.
- The current draft names `eval-5dbf5632` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: define stable logical and physical identities, explicit grain and keys, real foreign keys, deliberate cascades, rich column metadata, and migration-owned evolution.
- A success claim must be supported by the model graph, constraints, migration revision, governed operation result, and ownership boundary.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
