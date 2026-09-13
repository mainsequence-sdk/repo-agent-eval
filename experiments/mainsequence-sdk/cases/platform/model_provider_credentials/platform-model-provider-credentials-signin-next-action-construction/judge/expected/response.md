The plan should make **Follow sign-in next actions** the central decision: Present and process the supported next action while preserving secrets and attempt identity.

For `eval-91a0f747`, first freeze the repository and environment identities and resolve every referenced resource rather than guessing identifiers. Then apply the selected skill's ordered operations. The governing boundary is: discover provider/model capability, distinguish built-in from custom ownership, inspect status, use a durable sign-in attempt and next_action loop, and preserve secrecy. The plan should preserve the distinction between configuration, the concrete operation or release, and later runtime state.

The response should explicitly reject shortcuts that erase ownership or identity boundaries. It must not expose credentials, invent observations, use removed APIs, or report a live outcome from this response-only task.

The closing evidence checklist should include provider and model identity, credential status, attempt identity and next action, explicit terminal state, and redacted cancellation or revocation result. Static review can establish that the plan and declarations conform to the skill; only observed platform state, logs, health, and representative behavior can establish successful execution where those observations apply.
