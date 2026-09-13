The plan should make **Configure an update** the central decision: Use TimeIndexTableUpdateConfig and TimeIndexTableUpdate to define current update behavior.

For `eval-c41ac574`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: keep TimeIndexMetaTable storage separate from TimeIndexTableUpdater execution, use current update objects, compute per-entity windows, and emit UTC nanosecond-indexed output. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include updater identity and hash, dependency graph, requested windows, canonical output schema, scoped tail replacement, and stored progress. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
