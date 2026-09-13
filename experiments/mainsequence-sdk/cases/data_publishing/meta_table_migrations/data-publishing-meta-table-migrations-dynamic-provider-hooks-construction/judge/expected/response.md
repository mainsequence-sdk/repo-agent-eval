The plan should make **Implement dynamic provider hooks** the central decision: Apply the supported hooks for provider discovery and runtime migration integration.

For `eval-1ee69b1f`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: use the supported Alembic provider package, stable registry root, ordered revisions, and the reserved-to-active lifecycle without direct runtime registration. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include provider discovery, revision ordering, upgrade outcome, registry state, and post-migration schema verification. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
