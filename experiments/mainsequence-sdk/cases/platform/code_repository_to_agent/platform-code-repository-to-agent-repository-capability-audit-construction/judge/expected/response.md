The plan should make **Audit repository agent readiness** the central decision: Determine whether a repository has the code, instructions, and interfaces needed to become an Agent.

For `eval-7cf1b347`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: audit readiness, keep repository instructions and skills durable, separate source and runtime Agent cards, use a stable non-mutating runtime command, and declare automatic deployment through the workflow. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include prepared source revision, validated workflow target, backend image build, release revision, DeploymentRun, Agent reachability, and session provenance. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
