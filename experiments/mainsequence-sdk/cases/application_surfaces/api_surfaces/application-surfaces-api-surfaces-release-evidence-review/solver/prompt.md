# Goal
Review the proposed `eval-20db30cb` design for this scenario: The team is working on a portfolio application consumed by the pinned Command Center SDK. The requested slice is 'Establish release evidence' and the concrete outcome is to distinguish declaration, release, health, data proof, and deployed behavior evidence.

# Supplied evidence
- Required behavior: Distinguish declaration, release, health, data proof, and deployed behavior evidence.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
- The governing boundary is: use the authoritative frontend contract, accept identity only from platform-injected request state, keep transport thin, and separate release health from payload conformance.
- A success claim must be supported by schema and fixture conformance, trusted-request identity handling, release state, health, and one representative data behavior.
- Proposed implementation: The current proposal accepts a caller uid in request JSON, copies a convenient project payload instead of the pinned schema, and claims success from a health response.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
