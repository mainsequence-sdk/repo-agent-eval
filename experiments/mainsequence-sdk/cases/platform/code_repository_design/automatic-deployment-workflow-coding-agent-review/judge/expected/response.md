The review should reject the proposal because it violates the **Automatic deployment for workflow coding agent** boundary. The required behavior is: Apply the correct deployment and proof contract for a CodeRepository Coding Agent declared by a repository workflow.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. Apply this target-specific image rule: the backend builds its image and the workflow does not supply one. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The proposed shortcut applies one generic image field to every target and declares success immediately after committing the workflow file. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires prepared source revision, workflow validation, backend build, DeploymentRun, and a bounded Agent task.
