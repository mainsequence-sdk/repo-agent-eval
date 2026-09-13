# Goal
Review the proposed `eval-dee9ed01` design for this scenario: The team is working on an analyst looking for account positions and minute bars without known resource uids. The requested slice is 'Preserve evidence limits' and the concrete outcome is to state what observed catalog evidence proves, what remains ambiguous, and what must be verified.

# Supplied evidence
- Required behavior: State what observed catalog evidence proves, what remains ambiguous, and what must be verified.
- One local failure and one upstream source excerpt are supplied; neither establishes live platform state or issue ownership by itself.
- The governing boundary is: classify MetaTable versus time-index storage first, discover semantically, narrow with supported filters and columns, inspect details, and preserve ambiguity.
- A success claim must be supported by the observed catalog candidates, exact selected identity and contract, rejected alternatives, and unresolved facts.
- Proposed implementation: The current proposal guesses a uid and starts implementation after a name match without checking storage surface, grain, columns, or namespace.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
