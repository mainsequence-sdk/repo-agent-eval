The plan should make **Select the exact Job image** the central decision: Bind each Job to the correct immutable runtime image and ownership source.

For `eval-29722638`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: separate workflow, Job, schedule, JobRun, image, artifact, resource, release, revision, and DeploymentRun identities and require the full proof chain. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include workflow validation, exact image, JobRun logs/usage, artifact identity, release revision, DeploymentRun, health, and representative output. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
