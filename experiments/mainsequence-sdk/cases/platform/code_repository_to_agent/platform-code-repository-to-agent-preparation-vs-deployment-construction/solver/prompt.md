# Goal
Design the `eval-9c87ddd3` implementation slice for this scenario: The team is working on a repository being prepared and deployed as a CodeRepository Coding Agent. The requested slice is 'Separate preparation from deployment' and the concrete outcome is to distinguish repository changes and validation from platform workflow declaration and release evidence.

# Supplied evidence
- The repository revision and target environment have already been selected; their concrete identifiers are intentionally represented by `eval-9c87ddd3-repo` and `eval-9c87ddd3-env`.
- Automatic deployment is requested, but the supplied draft uses the same image field and success check for every target type.
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
