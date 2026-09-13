# Goal
Review the proposed `eval-6dbc373e` design for this scenario: The team is working on an operator connecting a supported model provider for an Agent runtime. The requested slice is 'Distinguish built-in and custom providers' and the concrete outcome is to apply the correct configuration and ownership path for platform-managed versus custom providers.

# Supplied evidence
- Required behavior: Apply the correct configuration and ownership path for platform-managed versus custom providers.
- The current draft names `eval-6dbc373e` but does not yet separate declaration, owned operation, and observable runtime result.
- The governing boundary is: discover provider/model capability, distinguish built-in from custom ownership, inspect status, use a durable sign-in attempt and next_action loop, and preserve secrecy.
- A success claim must be supported by provider and model identity, credential status, attempt identity and next action, explicit terminal state, and redacted cancellation or revocation result.
- Proposed implementation: The current proposal asks the user to paste a secret into chat, stores it in repository YAML, assumes sign-in initiation means success, and deletes every provider credential on cancellation.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
