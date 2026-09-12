# MainSequence SDK Case Bank

Cases are grouped by the exact configured skill id. Each case package contains
`case.yaml`, a self-contained solver prompt, a hidden expert response, and an
LLM-judge rubric. `splits.yaml` assigns whole semantic leakage groups using the
workspace's deterministic seed.

`coverage.yaml` is the complete semantic contract. It decomposes each selected
skill into current-API capabilities and requires three primary cases per
capability. Every case has exactly one primary capability and at most two
supporting capabilities. Supporting tags do not earn readiness credit.

This first foundational campaign has two cases and two independent leakage
groups for every skill:

| Skill | Cases |
| --- | --- |
| `a2a_sdk_execution` | `a2a-stable-session-followup`, `a2a-task-capability-boundary` |
| `application_surfaces/api_surfaces` | `api-trusted-request-identity`, `api-contract-evidence-ladder` |
| `data_access/exploration` | `exploration-semantic-search-plan`, `exploration-storage-surface-handoff` |
| `data_publishing/meta_table_migrations` | `migration-provider-lifecycle`, `migration-registration-boundary` |
| `data_publishing/meta_tables` | `metatable-related-model-graph`, `metatable-identity-contract` |
| `data_publishing/time_index_table_updates` | `updater-producer-reference-graph`, `updater-output-contract-boundary` |
| `maintenance/bug_auditor` | `audit-time-index-dtype-failure`, `audit-unregistered-branch-failure` |
| `maintenance/code_repository_maintenance` | `maintenance-safe-sdk-refresh`, `maintenance-partial-sync-recovery` |
| `platform_operations/access_control_and_sharing` | `sharing-published-output-consumer`, `sharing-secret-idempotent-grant` |
| `platform_operations/orchestration_and_releases` | `orchestration-manual-automatic-image`, `orchestration-workflow-proof-chain` |
| `sdk_code_repository_execution` | `repository-git-context-bootstrap`, `repository-specialized-skill-routing` |

This first round is only an initial classification, not skill coverage. The
workspace is intentionally incomplete because each capability requires three
primary cases in addition to the 12-case and 6-group per-skill floors. No
solver or judge call is allowed until all semantic gaps and judge calibration
are complete.
