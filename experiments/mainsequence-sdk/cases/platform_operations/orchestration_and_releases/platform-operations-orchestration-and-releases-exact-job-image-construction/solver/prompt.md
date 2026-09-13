# Goal
Design the `eval-29722638` implementation slice for this scenario: The team is working on a scheduled analytics workflow producing an artifact and serving a released application. The requested slice is 'Select the exact Job image' and the concrete outcome is to bind each Job to the correct immutable runtime image and ownership source.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-29722638-repo` and `eval-29722638-env`.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- The governing boundary is: separate workflow, Job, schedule, JobRun, image, artifact, resource, release, revision, and DeploymentRun identities and require the full proof chain.
- A success claim must be supported by workflow validation, exact image, JobRun logs/usage, artifact identity, release revision, DeploymentRun, health, and representative output.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
