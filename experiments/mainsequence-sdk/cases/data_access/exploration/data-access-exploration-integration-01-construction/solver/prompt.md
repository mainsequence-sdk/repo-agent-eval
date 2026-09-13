# Goal
Design the `eval-6b1d8bf6` implementation slice for this scenario: The team is working on an analyst looking for account positions and minute bars without known resource uids. The requested slice is 'Classify storage surfaces' and the concrete outcome is to distinguish row-oriented MetaTables from time-indexed data before choosing discovery operations.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-6b1d8bf6-repo` and `eval-6b1d8bf6-env`.
- Asset A is current through 10:00 UTC, asset B through 09:41 UTC, and a bounded correction starts at 09:55 UTC for A only.
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
