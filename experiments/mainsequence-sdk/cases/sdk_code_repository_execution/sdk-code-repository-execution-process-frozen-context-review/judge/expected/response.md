The review should reject the proposal because it violates the **Freeze process context** boundary. The required behavior is: Establish stable execution context before commands or platform interactions begin.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: read repository instructions first, derive remote/branch/worktree context from Git, freeze it before operations, route specialized work, define success evidence, and stop on material ambiguity. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal takes branch and environment from ad-hoc environment variables, skips nested instructions, absorbs domain-specific work, and equates command exit zero with task success. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires Git-native identity, applied instruction chain, selected skill route, bounded commands, and the observations in the task's success definition.
