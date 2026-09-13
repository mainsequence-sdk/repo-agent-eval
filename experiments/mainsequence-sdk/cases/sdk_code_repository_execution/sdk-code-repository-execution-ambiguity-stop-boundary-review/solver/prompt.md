# Goal
Review the proposed `eval-bf0f30ba` design for this scenario: The team is working on an SDK repository task executed from a feature worktree. The requested slice is 'Stop on material ambiguity' and the concrete outcome is to identify missing authority or unsafe assumptions and request the smallest necessary clarification.

# Supplied evidence
- Required behavior: Identify missing authority or unsafe assumptions and request the smallest necessary clarification.
- The current draft names `eval-bf0f30ba` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: read repository instructions first, derive remote/branch/worktree context from Git, freeze it before operations, route specialized work, define success evidence, and stop on material ambiguity.
- A success claim must be supported by Git-native identity, applied instruction chain, selected skill route, bounded commands, and the observations in the task's success definition.
- Proposed implementation: The current proposal takes branch and environment from ad-hoc environment variables, skips nested instructions, absorbs domain-specific work, and equates command exit zero with task success.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
