# Goal
Design the `eval-3875e4f8` implementation slice for this scenario: The team is working on a developer materializing a platform CodeRepository branch into a clean local workspace. The requested slice is 'Separate authentication lanes' and the concrete outcome is to keep Git clone authentication, CLI user authentication, and runtime credentials distinct.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-3875e4f8-repo` and `eval-3875e4f8-env`.
- The provider listing and credential status are visible, but no secret value is present and sign-in is not yet terminal.
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
