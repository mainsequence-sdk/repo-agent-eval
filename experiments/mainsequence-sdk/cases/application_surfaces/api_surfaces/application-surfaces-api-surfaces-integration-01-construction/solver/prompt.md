# Goal
Design the `eval-7a1220ef` implementation slice for this scenario: The team is working on a portfolio application consumed by the pinned Command Center SDK. The requested slice is 'Classify the endpoint surface' and the concrete outcome is to distinguish health, metadata, and application data endpoints and their evidence obligations.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-7a1220ef-repo` and `eval-7a1220ef-env`.
- The current draft names `eval-7a1220ef` but does not yet separate declaration, owned operation, and observable runtime result.
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
