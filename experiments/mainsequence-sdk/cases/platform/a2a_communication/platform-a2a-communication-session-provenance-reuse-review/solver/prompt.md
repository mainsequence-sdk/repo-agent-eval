# Goal
Review the proposed `eval-70d4d1ff` design for this scenario: The team is working on a private caller delegating analysis to an existing Agent in a named organization environment. The requested slice is 'Reuse sessions with provenance' and the concrete outcome is to reuse only sessions whose caller, target, environment, and purpose match the request.

# Supplied evidence
- Required behavior: Reuse only sessions whose caller, target, environment, and purpose match the request.
- A first request already returned session `eval-70d4d1ff-session`; a follow-up and one retry are now required.
- The governing boundary is: resolve the environment-scoped Agent, prove caller and stable session provenance, update runtime access atomically when required, preserve message identity, and never expose credentials.
- A success claim must be supported by environment, target Agent, private caller session, message id, advertised response kind, task terminal state, and redacted access result.
- Proposed implementation: The current proposal searches across environments, trusts a public caller uid, opens unrelated sessions on retry, and prints the communication token for debugging.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
