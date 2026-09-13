# Goal
Design the `eval-9156b028` implementation slice for this scenario: The team is working on a Vite Command Center application backed by an authenticated FastAPI release. The requested slice is 'Verify Static Site deployment' and the concrete outcome is to check build state, deployed assets, routing, navigation, and user-visible behavior.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-9156b028-repo` and `eval-9156b028-env`.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- The governing boundary is: use the installed Command Center SDK, keep delivery static, use opaque platform routing, delegate dynamic work to FastAPI, configure CORS/navigation, and enable branch-driven builds.
- A success claim must be supported by build output and revision, DeploymentRun logs, opaque route resolution, navigation visibility, CORS preflight, and one user-visible API-backed behavior.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
