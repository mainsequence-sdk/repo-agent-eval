# Goal
Design the `eval-5079ac27` implementation slice for this scenario: The team is working on a multi-asset one-minute market-data pipeline with incremental backfills. The requested slice is 'Preserve storage metadata' and the concrete outcome is to keep table metadata, logical identity, physical identity, and updater configuration boundaries coherent.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-5079ac27-repo` and `eval-5079ac27-env`.
- Two visible resources share the label `eval-5079ac27`, but their stable uids, namespaces, and owners differ.
- The governing boundary is: keep TimeIndexMetaTable storage separate from TimeIndexTableUpdater execution, use current update objects, compute per-entity windows, and emit UTC nanosecond-indexed output.
- A success claim must be supported by updater identity and hash, dependency graph, requested windows, canonical output schema, scoped tail replacement, and stored progress.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
