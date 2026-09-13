# Goal
Design the `eval-dab5569f` implementation slice for this scenario: The team is working on an organization promoting one repository from development to production. The requested slice is 'Preserve organization ownership' and the concrete outcome is to treat environments as organization-owned partitions rather than local labels.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-dab5569f-repo` and `eval-dab5569f-env`.
- The current draft names `eval-dab5569f` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: treat environments as organization partitions, verify branch compatibility, require explicit human selection, derive runtime scope from the target, and promote code separately from configuration and data.
- A success claim must be supported by organization/environment identities, branch compatibility, isolated resource resolution, promotion records, and lifecycle state.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
