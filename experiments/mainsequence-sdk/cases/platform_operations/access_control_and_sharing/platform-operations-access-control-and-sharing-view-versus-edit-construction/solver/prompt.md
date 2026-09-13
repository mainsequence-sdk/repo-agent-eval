# Goal
Design the `eval-9675f0de` implementation slice for this scenario: The team is working on a team sharing one published, one Secret, and one published data resource with a consumer application. The requested slice is 'Select view or edit access' and the concrete outcome is to grant the least privilege required by the requested consumer behavior.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-9675f0de-repo` and `eval-9675f0de-env`.
- The requested consumer needs read-only use; the current ACL also contains unrelated team and owner grants that must survive.
- The governing boundary is: resolve stable public principals and actual resource identities, choose least privilege, separate team membership from grants, reconcile idempotently, and preserve unrelated access.
- A success claim must be supported by resolved principal/resource identities, requested and effective permissions, preserved grants, and redacted named-configuration state.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
