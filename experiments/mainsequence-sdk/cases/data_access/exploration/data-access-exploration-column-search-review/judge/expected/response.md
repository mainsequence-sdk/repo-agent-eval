The review should reject the proposal because it violates the **Search by column contract** boundary. The required behavior is: Discover resources using required columns, schemas, and grain constraints.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: classify MetaTable versus time-index storage first, discover semantically, narrow with supported filters and columns, inspect details, and preserve ambiguity. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal guesses a uid and starts implementation after a name match without checking storage surface, grain, columns, or namespace. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires the observed catalog candidates, exact selected identity and contract, rejected alternatives, and unresolved facts.
