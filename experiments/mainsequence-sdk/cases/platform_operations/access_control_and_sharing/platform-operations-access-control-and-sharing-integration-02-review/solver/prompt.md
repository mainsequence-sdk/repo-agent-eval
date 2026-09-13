# Goal
Review the proposed `eval-d0b944a9` design for this scenario: The team is working on a team sharing one published, one Secret, and one published data resource with a consumer application. The requested slice is 'Administer team membership' and the concrete outcome is to manage team membership separately from resource grants and preserve unrelated members.

# Supplied evidence
- Required behavior: Manage team membership separately from resource grants and preserve unrelated members.
- The requested consumer needs read-only use; the current ACL also contains unrelated team and owner grants that must survive.
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
