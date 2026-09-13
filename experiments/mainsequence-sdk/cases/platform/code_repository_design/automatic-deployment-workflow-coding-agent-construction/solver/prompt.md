# Goal
Design the `eval-e4793a64` implementation slice for this scenario: The repository must deliver a CodeRepository Coding Agent declared by a repository workflow.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-e4793a64-repo` and `eval-e4793a64-env`.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- Apply this target-specific image rule: the backend builds its image and the workflow does not supply one.
- Success must be established from prepared source revision, workflow validation, backend build, DeploymentRun, and a bounded Agent task.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
