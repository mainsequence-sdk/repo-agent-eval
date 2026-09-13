The plan should make **Evolve schemas through migrations** the central decision: Change owned MetaTable schemas through the supported migration lifecycle.

For `eval-c3568f95`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: define stable logical and physical identities, explicit grain and keys, real foreign keys, deliberate cascades, rich column metadata, and migration-owned evolution. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include the model graph, constraints, migration revision, governed operation result, and ownership boundary. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
