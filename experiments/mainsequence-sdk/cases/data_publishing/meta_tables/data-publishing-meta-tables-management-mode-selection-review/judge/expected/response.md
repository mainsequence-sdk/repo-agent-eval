The review should reject the proposal because it violates the **Select the management mode** boundary. The required behavior is: Choose the supported MetaTable ownership and management mode for the scenario.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: define stable logical and physical identities, explicit grain and keys, real foreign keys, deliberate cascades, rich column metadata, and migration-owned evolution. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal uses display names as identities, stores relationships as unrelated strings, omits composite uniqueness, and registers schemas at runtime. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires the model graph, constraints, migration revision, governed operation result, and ownership boundary.
