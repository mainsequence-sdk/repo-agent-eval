# Goal
Design the `eval-c370be5f` implementation slice for this scenario: The team is working on a repository workflow declaring Jobs, FastAPI, a runtime Agent, a Static Site, and a Coding Agent. The requested slice is 'Maintain the direct workflow file contract' and the concrete outcome is to keep workflow declarations in the supported repository location and schema.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-c370be5f-repo` and `eval-c370be5f-env`.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
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
