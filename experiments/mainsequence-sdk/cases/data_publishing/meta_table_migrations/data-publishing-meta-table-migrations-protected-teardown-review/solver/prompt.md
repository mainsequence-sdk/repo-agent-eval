# Goal
Review the proposed `eval-481367a6` design for this scenario: The team is working on a provider-owned portfolio schema evolving across repository releases. The requested slice is 'Protect destructive boundaries' and the concrete outcome is to handle downgrade and teardown behavior without unsafe or unowned destructive actions.

# Supplied evidence
- Required behavior: Handle downgrade and teardown behavior without unsafe or unowned destructive actions.
- The current draft names `eval-481367a6` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: use the supported Alembic provider package, stable registry root, ordered revisions, and the reserved-to-active lifecycle without direct runtime registration.
- A success claim must be supported by provider discovery, revision ordering, upgrade outcome, registry state, and post-migration schema verification.
- Proposed implementation: The current proposal creates tables at application startup, calls register directly, and treats management mode, schema-management mode, and lifecycle state as one flag.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
