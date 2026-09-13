# Goal
Design the `eval-8e470911` implementation slice for this scenario: The team is working on a repository releasing a FastAPI service, runtime Agent, and Static Site. The requested slice is 'Configure CORS deliberately' and the concrete outcome is to authorize only the intended frontend origins, methods, and headers, including preflight behavior.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-8e470911-repo` and `eval-8e470911-env`.
- The pinned contract and one representative fixture are supplied; a project-local payload differs in one field and no browser preflight has been observed.
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
