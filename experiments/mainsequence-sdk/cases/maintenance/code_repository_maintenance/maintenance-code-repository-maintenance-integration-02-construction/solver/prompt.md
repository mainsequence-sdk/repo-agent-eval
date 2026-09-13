# Goal
Design the `eval-a5b33e64` implementation slice for this scenario: The team is working on a Python 3.12 connector repository receiving an SDK and managed-skill refresh. The requested slice is 'Protect authentication material' and the concrete outcome is to choose the correct authentication lane without exposing or persisting credentials unsafely.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-a5b33e64-repo` and `eval-a5b33e64-env`.
- The provider listing and credential status are visible, but no secret value is present and sign-in is not yet terminal.
- The governing boundary is: freeze Git context, protect credentials, update dependency and lock state, refresh managed content from its canonical source, recover partial work, and report validation precisely.
- A success claim must be supported by branch and remote identity, dependency and lock diff, managed-source pin, focused tests, remaining failures, and exact changed state.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
