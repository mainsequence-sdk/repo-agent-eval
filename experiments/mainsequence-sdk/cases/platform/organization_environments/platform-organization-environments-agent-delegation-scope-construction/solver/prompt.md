# Goal
Design the `eval-f9765c15` implementation slice for this scenario: The team is working on an organization promoting one repository from development to production. The requested slice is 'Scope delegated Agent work' and the concrete outcome is to ensure delegated actions inherit the authorized environment rather than selecting another implicitly.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-f9765c15-repo` and `eval-f9765c15-env`.
- Two visible resources share the label `eval-f9765c15`, but their stable uids, namespaces, and owners differ.
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
