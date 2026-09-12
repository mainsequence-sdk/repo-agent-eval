Add `AlertRule` to the repository-owned SQLAlchemy model graph and include it in
the existing migration registry so the provider sees the complete owned graph.
Keep platform-managed tables under the Alembic/provider path; do not call
`.register()` during normal application startup and do not invent a raw
platform operation payload.

Use the Main Sequence migration CLI to inspect/scaffold the provider as needed,
confirm current state, generate a revision from the model change, and review
the generated upgrade and downgrade before applying it. The provider root has
no parent: it moves from `platform_managed` with lifecycle state `reserved` to
`alembic_managed` with lifecycle state `active` through the migration path.
Subsequent schema changes remain owned by that provider and revision chain.

Before upgrade, verify the intended registered CodeRepositoryBranch context,
provider identity, revision ancestry, table names, keys, constraints, foreign
keys, and destructive operations. After an authorized upgrade, evidence should
include provider/current revision state, the applied revision, inspected
MetaTable registration/schema, and relevant application tests. In this task
those are required future checks, not completed observations.
