# Goal
Design the `eval-1b5eff38` implementation slice for this scenario: The team is working on an operator connecting a supported model provider for an Agent runtime. The requested slice is 'Inspect credential status' and the concrete outcome is to determine whether usable provider credentials already exist without exposing their values.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-1b5eff38-repo` and `eval-1b5eff38-env`.
- The provider listing and credential status are visible, but no secret value is present and sign-in is not yet terminal.
- The governing boundary is: discover provider/model capability, distinguish built-in from custom ownership, inspect status, use a durable sign-in attempt and next_action loop, and preserve secrecy.
- A success claim must be supported by provider and model identity, credential status, attempt identity and next action, explicit terminal state, and redacted cancellation or revocation result.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
