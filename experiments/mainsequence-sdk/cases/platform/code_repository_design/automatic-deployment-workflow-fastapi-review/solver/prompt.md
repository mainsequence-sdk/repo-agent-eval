# Goal
Review the proposed `eval-969fd8cd` design for this scenario: The repository must deliver a workflow FastAPI resource exposing REST and WebSocket routes.

# Supplied evidence
- Required behavior: Apply the correct deployment and proof contract for a workflow FastAPI resource exposing REST and WebSocket routes.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- Apply this target-specific image rule: automatic deployment omits related_image_uid, while disabled automation requires an exact ready image.
- Success must be established from release revision, DeploymentRun, health, REST contract, and WebSocket behavior.
- Proposed implementation: The proposed shortcut applies one generic image field to every target and declares success immediately after committing the workflow file.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
