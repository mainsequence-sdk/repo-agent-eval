The plan should make **Preserve datasource routing boundaries** the central decision: Keep environment selection distinct from datasource or provider routing decisions.

For `eval-6d01b23c`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: treat environments as organization partitions, verify branch compatibility, require explicit human selection, derive runtime scope from the target, and promote code separately from configuration and data. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include organization/environment identities, branch compatibility, isolated resource resolution, promotion records, and lifecycle state. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
