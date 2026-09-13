# Goal
Review the proposed `eval-daee2100` design for this scenario: The team is working on a repository releasing a FastAPI service, runtime Agent, and Static Site. The requested slice is 'Preserve static-site image semantics' and the concrete outcome is to let the backend build Static Site assets without attaching a runtime image.

# Supplied evidence
- Required behavior: Let the backend build Static Site assets without attaching a runtime image.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- The governing boundary is: select the correct release kind, preserve revision lifecycle, apply direct-versus-workflow image rules, configure CORS and redeployment, and observe DeploymentRuns before claiming success.
- A success claim must be supported by resource and release kind, immutable revision, image/build owner, DeploymentRun and logs, health, contract behavior, and safe retirement state.
- Proposed implementation: The current proposal omits an image from direct runtime creation because auto-redeploy is true, attaches an image to a Static Site, conflates release and revision, and treats health as behavior proof.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
