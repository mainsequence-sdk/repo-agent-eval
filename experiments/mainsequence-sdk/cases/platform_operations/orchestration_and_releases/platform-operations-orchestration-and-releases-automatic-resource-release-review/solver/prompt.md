# Goal
Review the proposed `eval-f0e2f63b` design for this scenario: The team is working on a scheduled analytics workflow producing an artifact and serving a released application. The requested slice is 'Automate resource releases' and the concrete outcome is to configure safe automatic release behavior without treating declaration as completed deployment.

# Supplied evidence
- Required behavior: Configure safe automatic release behavior without treating declaration as completed deployment.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- The governing boundary is: separate workflow, Job, schedule, JobRun, image, artifact, resource, release, revision, and DeploymentRun identities and require the full proof chain.
- A success claim must be supported by workflow validation, exact image, JobRun logs/usage, artifact identity, release revision, DeploymentRun, health, and representative output.
- Proposed implementation: The current proposal uses one mutable image tag, conflates a schedule with a run, treats a pushed declaration as deployment, and claims a healthy service produced correct data.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
