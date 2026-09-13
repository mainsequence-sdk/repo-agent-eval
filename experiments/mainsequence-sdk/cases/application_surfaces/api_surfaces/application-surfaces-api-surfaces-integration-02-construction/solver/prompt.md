# Goal
Design the `eval-6524a332` implementation slice for this scenario: The team is working on a portfolio application consumed by the pinned Command Center SDK. The requested slice is 'Use trusted caller identity' and the concrete outcome is to read platform-injected identity safely without accepting spoofable caller input.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-6524a332-repo` and `eval-6524a332-env`.
- Two visible resources share the label `eval-6524a332`, but their stable uids, namespaces, and owners differ.
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
