Keep `sdk_code_repository_execution` responsible for the agreed success
condition, docs/read order, verified Git-derived context, and coordination only.
Route the implementation as follows:

1. `data_publishing/meta_tables` owns `AlertRule` grain, keys, logical/physical
   identity, relationships, descriptions, and provider placement. If its
   platform-managed schema changes, `data_publishing/meta_table_migrations`
   owns the reviewed provider-scoped revision and upgrade evidence.
2. `application_surfaces/api_surfaces` owns per-endpoint classification,
   trusted request identity, service delegation, the pinned Command Center SDK
   manifest/schema/fixtures, route conformance, release, and deployed frontend
   evidence.
3. `platform_operations/orchestration_and_releases` owns the nightly Job or
   versioned workflow, schedule, exact pinned image, JobRun, terminal status,
   and owner-scoped logs. It does not define alert calculations.
4. `platform_operations/access_control_and_sharing` owns the real output
   resource boundary, analyst-team public UID, least-privilege `view` grant,
   preservation of unrelated access, and effective consumer verification.

Before starting, write one integrated observable outcome: the model and
migration are valid, the exact tested API contract is consumed through the
deployed frontend, the nightly Job runs the intended pushed commit, and the
team can read—but not edit—the intended result resource. Each claim remains
open until its owning skill returns the specified evidence. Do not let the
bootstrap skill substitute its own rules for those domain contracts.
