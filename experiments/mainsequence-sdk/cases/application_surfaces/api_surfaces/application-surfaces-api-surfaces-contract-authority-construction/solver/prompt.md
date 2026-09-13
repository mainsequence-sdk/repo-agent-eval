# Goal
Design the `eval-283841f9` implementation slice for this scenario: The team is working on a portfolio application consumed by the pinned Command Center SDK. The requested slice is 'Select the authoritative contract' and the concrete outcome is to resolve the normative Command Center request and response contract before implementation.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-283841f9-repo` and `eval-283841f9-env`.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
- The governing boundary is: use the authoritative frontend contract, accept identity only from platform-injected request state, keep transport thin, and separate release health from payload conformance.
- A success claim must be supported by schema and fixture conformance, trusted-request identity handling, release state, health, and one representative data behavior.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
