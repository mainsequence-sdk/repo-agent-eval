# Goal
Design the `eval-bcf2d3ee` implementation slice for this scenario: The team is working on an organization promoting one repository from development to production. The requested slice is 'Promote code between environments' and the concrete outcome is to promote repository revisions through supported branch and release paths.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-bcf2d3ee-repo` and `eval-bcf2d3ee-env`.
- Branch `eval-bcf2d3ee-feature` is ready in development; production has different resource and configuration identities.
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
