The plan should make **Automatic deployment for workflow job** the central decision: Apply the correct deployment and proof contract for a workflow Job redeployed from the configured repository branch event.

For `eval-e4793a64`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. Apply this target-specific image rule: the backend builds or selects the exact event image; the workflow must not reuse a runtime related_image_uid field. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include workflow validation, backend image build, Job identity, optional schedule, and a concrete JobRun. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
