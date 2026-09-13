The plan should make **Establish Git context** the central decision: Inspect branch, remote, worktree, and repository state before maintenance actions.

For `eval-7f55182d`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: freeze Git context, protect credentials, update dependency and lock state, refresh managed content from its canonical source, recover partial work, and report validation precisely. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include branch and remote identity, dependency and lock diff, managed-source pin, focused tests, remaining failures, and exact changed state. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
