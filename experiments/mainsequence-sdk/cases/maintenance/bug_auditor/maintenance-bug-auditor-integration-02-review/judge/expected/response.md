The review should reject the proposal because it violates the **Classify ownership** boundary. The required behavior is: Distinguish target SDK, project, environment, platform, and reference-project ownership.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: separate observation from hypothesis, localize the failing ownership boundary, inspect authoritative upstream evidence, avoid duplicates, and propose the smallest verification. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal mutates the checkout, labels the SDK defective from a downstream stack trace alone, and files a duplicate issue without ownership evidence. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires reproducible observations, exact source locations, ownership classification, competing hypotheses, duplicate search, and bounded next action.
