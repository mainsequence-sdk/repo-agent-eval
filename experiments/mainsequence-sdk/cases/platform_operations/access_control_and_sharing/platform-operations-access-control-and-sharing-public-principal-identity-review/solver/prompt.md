# Goal
Review the proposed `eval-595a8345` design for this scenario: The team is working on a team sharing one published, one Secret, and one published data resource with a consumer application. The requested slice is 'Resolve public principal identity' and the concrete outcome is to use stable public principal identifiers and never guess private or mutable identities.

# Supplied evidence
- Required behavior: Use stable public principal identifiers and never guess private or mutable identities.
- Two visible resources share the label `eval-595a8345`, but their stable uids, namespaces, and owners differ.
- The governing boundary is: resolve stable public principals and actual resource identities, choose least privilege, separate team membership from grants, reconcile idempotently, and preserve unrelated access.
- A success claim must be supported by resolved principal/resource identities, requested and effective permissions, preserved grants, and redacted named-configuration state.
- Proposed implementation: The current proposal guesses a user uid, grants edit when view is sufficient, replaces the full ACL, and treats a Secret name and Secret value as interchangeable.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
