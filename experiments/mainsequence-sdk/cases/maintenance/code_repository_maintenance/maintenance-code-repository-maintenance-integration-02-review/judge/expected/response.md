The review should reject the proposal because it violates the **Update the SDK dependency** boundary. The required behavior is: Change the SDK pin through the supported dependency and lock workflow.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: freeze Git context, protect credentials, update dependency and lock state, refresh managed content from its canonical source, recover partial work, and report validation precisely. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal runs a blind global upgrade, edits copied managed skills by hand, discards unrelated changes, and calls a successful install proof of application behavior. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires branch and remote identity, dependency and lock diff, managed-source pin, focused tests, remaining failures, and exact changed state.
