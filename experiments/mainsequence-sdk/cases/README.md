# MainSequence SDK Case Bank

Cases are grouped by the exact configured skill id. Each case package contains
coordinator-owned `case.yaml`, a public `solver/` surface, and a private
`judge/` surface containing the rubric and expected evidence. The coordinator
materializes only the relevant surface for each stage; it never mounts the
whole package into a model runtime. `splits.yaml` assigns whole semantic leakage
groups using the workspace's deterministic seed.

`coverage.yaml` is the complete semantic contract. It decomposes all 20
SDK-owned and MCP-imported skills into 193 current-API capabilities and requires
two primary cases per capability. Every case has exactly one primary capability
and at most two supporting capabilities. Supporting tags do not earn readiness
credit.

This first foundational campaign has two cases and two independent leakage
groups for every SDK-owned skill:

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

The complete bank contains 452 cases across 236 leakage groups. Every skill has
at least 20 cases and 10 groups, and every capability has at least two primary
cases. Extra `platform/code_repository_design` families cover automatic
deployment independently for standalone Jobs, workflow Jobs, FastAPI, runtime
Agents, Static Sites, and CodeRepository Coding Agents. Grouped splits are
deterministic, and the judge-validation corpus references an eligible training
group.
