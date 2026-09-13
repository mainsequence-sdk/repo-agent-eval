The review should reject the proposal because it violates the **Preserve organization ownership** boundary. The required behavior is: Treat environments as organization-owned partitions rather than local labels.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: treat environments as organization partitions, verify branch compatibility, require explicit human selection, derive runtime scope from the target, and promote code separately from configuration and data. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal lets deployed code choose an environment uid from a request, reuses development secrets and MetaTable uids in production, and calls a branch merge full environment promotion. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires organization/environment identities, branch compatibility, isolated resource resolution, promotion records, and lifecycle state.
