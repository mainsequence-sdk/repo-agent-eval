The review should reject the proposal because it violates the **Keep endpoints thin** boundary. The required behavior is: Separate FastAPI transport concerns from reusable service and domain behavior.

The corrected design should start from explicit repository, environment, resource, and actor identities, then follow the owning skill's supported sequence. The governing boundary is: use the authoritative frontend contract, accept identity only from platform-injected request state, keep transport thin, and separate release health from payload conformance. It must preserve unrelated state and must not substitute a convenient local assumption for an authoritative resource or lifecycle observation.

The answer should call out this concrete anti-pattern: The current proposal accepts a caller uid in request JSON, copies a convenient project payload instead of the pinned schema, and claims success from a health response. A semantically equivalent correction is acceptable when it preserves the same ownership, identity, lifecycle, and safety boundaries.

The review can prove only that the corrected design is aligned with the supplied contract. A success claim additionally requires schema and fixture conformance, trusted-request identity handling, release state, health, and one representative data behavior.
