# Goal
Design the `eval-e6617059` implementation slice for this scenario: The team is working on an analyst looking for account positions and minute bars without known resource uids. The requested slice is 'Perform semantic discovery' and the concrete outcome is to use semantic catalog discovery to identify plausible resources without inventing identifiers.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-e6617059-repo` and `eval-e6617059-env`.
- The current draft names `eval-e6617059` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: classify MetaTable versus time-index storage first, discover semantically, narrow with supported filters and columns, inspect details, and preserve ambiguity.
- A success claim must be supported by the observed catalog candidates, exact selected identity and contract, rejected alternatives, and unresolved facts.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
