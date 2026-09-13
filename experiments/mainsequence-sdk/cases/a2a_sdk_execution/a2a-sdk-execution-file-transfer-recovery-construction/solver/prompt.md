# Goal
Design the `eval-4fe5ca22` implementation slice for this scenario: The team is working on an already deployed research Agent serving the staging environment. The requested slice is 'Handle file transfer and recovery' and the concrete outcome is to apply the documented file-transfer, retry, and recovery boundaries for A2A communication.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-4fe5ca22-repo` and `eval-4fe5ca22-env`.
- The current draft names `eval-4fe5ca22` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: discover the existing Agent, preserve one AgentSession uid across related turns, honor its advertised message or task capabilities, and keep retries idempotent.
- A success claim must be supported by the selected Agent identity, stable session, response envelope or terminal task state, and bounded failure evidence.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
