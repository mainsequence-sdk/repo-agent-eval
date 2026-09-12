# Task

A registered Python repository owns several Platform-managed MetaTable models.
It is adding a row-oriented `AlertRule` model. The current migration registry
returns all repository-owned model classes through
`build_metatable_model_registry(..., base=Base)`. A teammate proposes:

1. call `AlertRule.register()` at application startup;
2. hand-write the platform operation payload;
3. run the application and assume the schema now exists.

Provide the correct migration-first sequence from model/registry update through
revision review and upgrade. Include the provider lifecycle state that must be
established and the evidence needed before calling the change complete. This
is a response-only plan; do not say the migration ran.
