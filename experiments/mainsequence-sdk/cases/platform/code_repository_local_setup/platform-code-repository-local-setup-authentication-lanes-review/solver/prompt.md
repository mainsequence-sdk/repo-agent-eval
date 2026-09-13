# Goal
Review the proposed `eval-c831d282` design for this scenario: The team is working on a developer materializing a platform CodeRepository branch into a clean local workspace. The requested slice is 'Separate authentication lanes' and the concrete outcome is to keep Git clone authentication, CLI user authentication, and runtime credentials distinct.

# Supplied evidence
- Required behavior: Keep Git clone authentication, CLI user authentication, and runtime credentials distinct.
- The provider listing and credential status are visible, but no secret value is present and sign-in is not yet terminal.
- The governing boundary is: select a READY repository and compatible branch, use the binding clone source, choose a safe destination, separate authentication lanes, and verify the resulting checkout.
- A success claim must be supported by repository and branch identity, binding readiness, remote and HEAD match, clean/safely preserved worktree state, and redacted authentication outcome.
- Proposed implementation: The current proposal reconstructs the clone URL, prints a deploy key, overwrites an unrelated checkout, mixes runtime credentials into .env, and continues after partial provisioning.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
