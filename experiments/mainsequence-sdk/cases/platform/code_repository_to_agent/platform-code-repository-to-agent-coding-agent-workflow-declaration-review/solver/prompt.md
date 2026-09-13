# Goal
Review the proposed `eval-40df8785` design for this scenario: The team is working on a repository being prepared and deployed as a CodeRepository Coding Agent. The requested slice is 'Declare a coding-agent workflow target' and the concrete outcome is to express the CodeRepository Coding Agent target with the required repository and environment context.

# Supplied evidence
- Required behavior: Express the CodeRepository Coding Agent target with the required repository and environment context.
- A workflow validates and a schedule is enabled, but no concrete JobRun, logs, usage record, or output artifact is supplied.
- The governing boundary is: audit readiness, keep repository instructions and skills durable, separate source and runtime Agent cards, use a stable non-mutating runtime command, and declare automatic deployment through the workflow.
- A success claim must be supported by prepared source revision, validated workflow target, backend image build, release revision, DeploymentRun, Agent reachability, and session provenance.
- Proposed implementation: The current proposal puts deployed URLs in the source card, installs dependencies on every start, skips repository instructions, and treats a pushed workflow file as a running Agent.
- The proposal reports no runtime logs, deployment run, terminal task state, or representative output beyond the statements above.

# Constraints
- Use only the current behavior in the supplied skill context.
- Treat the proposal as review input, not as evidence that an operation succeeded.
- Preserve unrelated resources, access, configuration, and repository state.
- Do not perform or claim live execution.

# Deliverable
Identify the material design errors, provide a corrected ordered design, and state exactly which observations would be required before success could be claimed.
