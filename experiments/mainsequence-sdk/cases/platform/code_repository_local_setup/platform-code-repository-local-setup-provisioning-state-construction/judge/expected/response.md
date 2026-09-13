The plan should make **Respect provisioning state** the central decision: Stop or recover safely when repository bindings, branch state, or credentials are not ready.

For `eval-9fb9dabe`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: select a READY repository and compatible branch, use the binding clone source, choose a safe destination, separate authentication lanes, and verify the resulting checkout. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include repository and branch identity, binding readiness, remote and HEAD match, clean/safely preserved worktree state, and redacted authentication outcome. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
