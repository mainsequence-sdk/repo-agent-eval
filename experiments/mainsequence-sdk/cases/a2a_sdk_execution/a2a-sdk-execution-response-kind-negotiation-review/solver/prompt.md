# Goal
Review the proposed `eval-f757254c` design for this scenario: The team is working on an already deployed research Agent serving the staging environment. The requested slice is 'Respect advertised response kinds' and the concrete outcome is to select only response modes and content kinds advertised by the remote Agent.

# Supplied evidence
- Required behavior: Select only response modes and content kinds advertised by the remote Agent.
- A first request already returned session `eval-f757254c-session`; a follow-up and one retry are now required.
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
