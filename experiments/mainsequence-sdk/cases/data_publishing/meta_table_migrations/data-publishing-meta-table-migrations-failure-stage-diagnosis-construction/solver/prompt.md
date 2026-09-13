# Goal
Design the `eval-da95bda7` implementation slice for this scenario: The team is working on a provider-owned portfolio schema evolving across repository releases. The requested slice is 'Diagnose migration stages' and the concrete outcome is to locate failures in discovery, ordering, execution, registration, or verification using supplied evidence.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-da95bda7-repo` and `eval-da95bda7-env`.
- One local failure and one upstream source excerpt are supplied; neither establishes live platform state or issue ownership by itself.
- The governing boundary is: use the supported Alembic provider package, stable registry root, ordered revisions, and the reserved-to-active lifecycle without direct runtime registration.
- A success claim must be supported by provider discovery, revision ordering, upgrade outcome, registry state, and post-migration schema verification.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
