The evidence analysis should center **Preserve storage metadata**: Keep table metadata, logical identity, physical identity, and updater configuration boundaries coherent.

Local configuration presence and schema validation support only a declaration-level conclusion. They do not prove that the intended identity was selected, that an owned operation completed, or that runtime behavior is correct. The governing boundary is: keep TimeIndexMetaTable storage separate from TimeIndexTableUpdater execution, use current update objects, compute per-entity windows, and emit UTC nanosecond-indexed output.

The unsupported column should explicitly reject live success or failure claims that require absent state. The response must preserve ambiguity instead of inventing uids, credentials, logs, releases, runs, or outputs.

The ordered observation column should request the narrowest relevant chain: updater identity and hash, dependency graph, requested windows, canonical output schema, scoped tail replacement, and stored progress. It should stop when an earlier identity, authorization, or lifecycle prerequisite fails and should preserve unrelated state.
