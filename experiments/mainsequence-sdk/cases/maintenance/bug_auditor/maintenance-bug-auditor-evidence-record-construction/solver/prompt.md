# Goal
Design the `eval-fded0d6b` implementation slice for this scenario: The team is working on a read-only audit of a connector failure with repository, SDK, and platform evidence. The requested slice is 'Build an evidence record' and the concrete outcome is to tie each finding to concrete supplied observations and distinguish fact from hypothesis.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-fded0d6b-repo` and `eval-fded0d6b-env`.
- One local failure and one upstream source excerpt are supplied; neither establishes live platform state or issue ownership by itself.
- The governing boundary is: separate observation from hypothesis, localize the failing ownership boundary, inspect authoritative upstream evidence, avoid duplicates, and propose the smallest verification.
- A success claim must be supported by reproducible observations, exact source locations, ownership classification, competing hypotheses, duplicate search, and bounded next action.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
