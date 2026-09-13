# Goal
Review the proposed `eval-5af5b73e` design for this scenario: The team is working on an already deployed research Agent serving the staging environment. The requested slice is 'Discover an existing Agent' and the concrete outcome is to locate and inspect an already deployed Agent before attempting communication.

# Supplied evidence
- Required behavior: Locate and inspect an already deployed Agent before attempting communication.
- The current draft names `eval-5af5b73e` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: discover the existing Agent, preserve one AgentSession uid across related turns, honor its advertised message or task capabilities, and keep retries idempotent.
- A success claim must be supported by the selected Agent identity, stable session, response envelope or terminal task state, and bounded failure evidence.
- Proposed implementation: The current proposal creates a new session for every follow-up, invents an unsupported response kind, and treats a transport acknowledgement as completed task output.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
