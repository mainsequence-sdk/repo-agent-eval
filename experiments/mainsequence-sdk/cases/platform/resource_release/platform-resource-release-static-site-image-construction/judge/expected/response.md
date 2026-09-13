The plan should make **Preserve static-site image semantics** the central decision: Let the backend build Static Site assets without attaching a runtime image.

For `eval-a0d9d66a`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: select the correct release kind, preserve revision lifecycle, apply direct-versus-workflow image rules, configure CORS and redeployment, and observe DeploymentRuns before claiming success. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include resource and release kind, immutable revision, image/build owner, DeploymentRun and logs, health, contract behavior, and safe retirement state. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
