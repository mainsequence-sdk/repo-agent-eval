# Goal
Design the `eval-4cbfd2c1` implementation slice for this scenario: The team is working on a repository workflow declaring Jobs, FastAPI, a runtime Agent, a Static Site, and a Coding Agent. The requested slice is 'Remove workflow applications safely' and the concrete outcome is to distinguish declaration removal, release cleanup, and protected resource deletion.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-4cbfd2c1-repo` and `eval-4cbfd2c1-env`.
- The current draft names `eval-4cbfd2c1` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: render and validate the current workflow contract, apply each target's exact image rule, keep schedules and navigation explicit, and inspect deployment runs.
- A success claim must be supported by backend validation, created resource and revision identities, image/build ownership, DeploymentRun state and logs, health, and target-specific behavior.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
