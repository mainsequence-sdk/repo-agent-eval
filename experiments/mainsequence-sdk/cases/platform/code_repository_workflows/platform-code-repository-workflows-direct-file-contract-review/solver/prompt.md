# Goal
Review the proposed `eval-f5a98bd5` design for this scenario: The team is working on a repository workflow declaring Jobs, FastAPI, a runtime Agent, a Static Site, and a Coding Agent. The requested slice is 'Maintain the direct workflow file contract' and the concrete outcome is to keep workflow declarations in the supported repository location and schema.

# Supplied evidence
- Required behavior: Keep workflow declarations in the supported repository location and schema.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
- The governing boundary is: render and validate the current workflow contract, apply each target's exact image rule, keep schedules and navigation explicit, and inspect deployment runs.
- A success claim must be supported by backend validation, created resource and revision identities, image/build ownership, DeploymentRun state and logs, health, and target-specific behavior.
- Proposed implementation: The current proposal uses one related_image_uid rule for every target, puts secrets in env_vars, omits timezone and navigation authorization, and claims deployment when validation passes.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
