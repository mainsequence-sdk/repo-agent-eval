# Goal
Design the `eval-ca4d3941` implementation slice for this scenario: The team is working on a repository releasing a FastAPI service, runtime Agent, and Static Site. The requested slice is 'Protect release deletion' and the concrete outcome is to distinguish stopping automation, retiring a release, and deleting underlying resources.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-ca4d3941-repo` and `eval-ca4d3941-env`.
- The current draft names `eval-ca4d3941` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: select the correct release kind, preserve revision lifecycle, apply direct-versus-workflow image rules, configure CORS and redeployment, and observe DeploymentRuns before claiming success.
- A success claim must be supported by resource and release kind, immutable revision, image/build owner, DeploymentRun and logs, health, contract behavior, and safe retirement state.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
