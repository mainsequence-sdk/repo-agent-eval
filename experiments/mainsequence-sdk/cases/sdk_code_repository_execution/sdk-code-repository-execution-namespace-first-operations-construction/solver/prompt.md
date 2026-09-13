# Goal
Design the `eval-60b165ba` implementation slice for this scenario: The team is working on an SDK repository task executed from a feature worktree. The requested slice is 'Resolve namespaces first' and the concrete outcome is to establish the correct namespace and resource context before platform operations.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-60b165ba-repo` and `eval-60b165ba-env`.
- The current draft names `eval-60b165ba` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: read repository instructions first, derive remote/branch/worktree context from Git, freeze it before operations, route specialized work, define success evidence, and stop on material ambiguity.
- A success claim must be supported by Git-native identity, applied instruction chain, selected skill route, bounded commands, and the observations in the task's success definition.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
