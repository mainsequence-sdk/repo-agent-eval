The review should reject the proposal because it violates the **Automatic deployment for standalone job** boundary. The required behavior is: Apply the correct deployment and proof contract for a directly managed automatic Job triggered by its supported event.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. Apply this target-specific image rule: the caller must select the exact ready Job image; automatic execution does not transfer direct image ownership to a runtime release. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The proposed shortcut applies one generic image field to every target and declares success immediately after committing the workflow file. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires Job declaration, trigger policy, JobRun state/logs, and produced output.
