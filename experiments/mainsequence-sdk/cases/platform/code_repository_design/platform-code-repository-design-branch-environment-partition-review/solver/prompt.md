# Goal
Review the proposed `eval-0247ac93` design for this scenario: The team is working on a repository that owns market data, scheduled computation, APIs, an Agent, and a web application. The requested slice is 'Partition branch and environment behavior' and the concrete outcome is to design branch-owned code and environment-owned state without cross-environment leakage.

# Supplied evidence
- Required behavior: Design branch-owned code and environment-owned state without cross-environment leakage.
- Branch `eval-0247ac93-feature` is ready in development; production has different resource and configuration identities.
- The governing boundary is: produce a connected ontology-aware blueprint with explicit owners, branch/environment partitions, data and execution edges, deployment modes, and success evidence.
- A success claim must be supported by the connected blueprint, validation evidence, release revisions, DeploymentRuns, runtime health, and representative behavior.
- Proposed implementation: The current proposal returns a folder tree with no resource identities, dependency graph, environment boundary, image policy, automatic deployment rule, or observable success definition.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
