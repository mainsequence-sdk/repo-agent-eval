# Goal
Design the `eval-ea8bc189` implementation slice for this scenario: The team is working on a read-only audit of a connector failure with repository, SDK, and platform evidence. The requested slice is 'Localize the root cause' and the concrete outcome is to connect an observable failure to the actual defective boundary rather than restating symptoms.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-ea8bc189-repo` and `eval-ea8bc189-env`.
- The current draft names `eval-ea8bc189` but does not yet separate declaration, owned operation, and observable runtime result.
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
