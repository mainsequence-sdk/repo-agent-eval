# Goal
Design the `eval-58c0f781` implementation slice for this scenario: The team is working on an operator connecting a supported model provider for an Agent runtime. The requested slice is 'Cancel or revoke safely' and the concrete outcome is to terminate obsolete attempts or credentials explicitly without damaging unrelated provider access.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-58c0f781-repo` and `eval-58c0f781-env`.
- The current draft names `eval-58c0f781` but does not yet separate declaration, owned operation, and observable runtime result.
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
