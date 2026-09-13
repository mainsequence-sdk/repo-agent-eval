# Goal
Design the `eval-d6055512` implementation slice for this scenario: The team is working on a private caller delegating analysis to an existing Agent in a named organization environment. The requested slice is 'Preserve message identity on retry' and the concrete outcome is to retry safely without turning one logical message into duplicate work.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-d6055512-repo` and `eval-d6055512-env`.
- A first request already returned session `eval-d6055512-session`; a follow-up and one retry are now required.
- The governing boundary is: resolve the environment-scoped Agent, prove caller and stable session provenance, update runtime access atomically when required, preserve message identity, and never expose credentials.
- A success claim must be supported by environment, target Agent, private caller session, message id, advertised response kind, task terminal state, and redacted access result.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
