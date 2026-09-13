# Goal
Design the `eval-9fb9dabe` implementation slice for this scenario: The team is working on a developer materializing a platform CodeRepository branch into a clean local workspace. The requested slice is 'Respect provisioning state' and the concrete outcome is to stop or recover safely when repository bindings, branch state, or credentials are not ready.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-9fb9dabe-repo` and `eval-9fb9dabe-env`.
- The current draft names `eval-9fb9dabe` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: select a READY repository and compatible branch, use the binding clone source, choose a safe destination, separate authentication lanes, and verify the resulting checkout.
- A success claim must be supported by repository and branch identity, binding readiness, remote and HEAD match, clean/safely preserved worktree state, and redacted authentication outcome.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
