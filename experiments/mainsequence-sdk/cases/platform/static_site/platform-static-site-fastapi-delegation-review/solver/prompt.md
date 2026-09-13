# Goal
Review the proposed `eval-2ab89469` design for this scenario: The team is working on a Vite Command Center application backed by an authenticated FastAPI release. The requested slice is 'Delegate dynamic behavior to FastAPI' and the concrete outcome is to keep Static Sites static and route authenticated dynamic operations through a release API.

# Supplied evidence
- Required behavior: Keep Static Sites static and route authenticated dynamic operations through a release API.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
- The governing boundary is: use the installed Command Center SDK, keep delivery static, use opaque platform routing, delegate dynamic work to FastAPI, configure CORS/navigation, and enable branch-driven builds.
- A success claim must be supported by build output and revision, DeploymentRun logs, opaque route resolution, navigation visibility, CORS preflight, and one user-visible API-backed behavior.
- Proposed implementation: The current proposal hard-codes a deployment URL, embeds server behavior in the static bundle, copies an old frontend contract, attaches a runtime image, and ignores browser preflight.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
