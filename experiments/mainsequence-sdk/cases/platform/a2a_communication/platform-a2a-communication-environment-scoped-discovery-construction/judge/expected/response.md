The plan should make **Discover agents in the target environment** the central decision: Resolve an existing Agent within the intended environment before opening a session.

For `eval-0bd4b27b`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: resolve the environment-scoped Agent, prove caller and stable session provenance, update runtime access atomically when required, preserve message identity, and never expose credentials. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include environment, target Agent, private caller session, message id, advertised response kind, task terminal state, and redacted access result. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
