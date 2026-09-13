# Goal
Design the `eval-ebf04f8f` implementation slice for this scenario: The team is working on a repository being prepared and deployed as a CodeRepository Coding Agent. The requested slice is 'Declare a coding-agent workflow target' and the concrete outcome is to express the CodeRepository Coding Agent target with the required repository and environment context.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-ebf04f8f-repo` and `eval-ebf04f8f-env`.
- A workflow validates and a schedule is enabled, but no concrete JobRun, logs, usage record, or output artifact is supplied.
- The governing boundary is: audit readiness, keep repository instructions and skills durable, separate source and runtime Agent cards, use a stable non-mutating runtime command, and declare automatic deployment through the workflow.
- A success claim must be supported by prepared source revision, validated workflow target, backend image build, release revision, DeploymentRun, Agent reachability, and session provenance.
- No execution, network, secret value, or live platform result is supplied.

# Constraints
- Use only the current APIs and behavior in the supplied skill context.
- Keep every resource identity and ownership boundary explicit.
- Do not claim that a declaration, local design, or health check proves later runtime behavior.
- Treat all credential values and private identities as unavailable.

# Deliverable
Return a compact ordered implementation plan. Name the required identities and operations, explain the central boundary, and finish with an evidence checklist that separates locally reviewable facts from live observations still required.
