The review should reject the proposal because it violates the **Prove browser CORS behavior** boundary. The required behavior is: Validate origin, preflight, credentials, and response headers for the deployed site/API pair.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: use the installed Command Center SDK, keep delivery static, use opaque platform routing, delegate dynamic work to FastAPI, configure CORS/navigation, and enable branch-driven builds. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal hard-codes a deployment URL, embeds server behavior in the static bundle, copies an old frontend contract, attaches a runtime image, and ignores browser preflight. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires build output and revision, DeploymentRun logs, opaque route resolution, navigation visibility, CORS preflight, and one user-visible API-backed behavior.
