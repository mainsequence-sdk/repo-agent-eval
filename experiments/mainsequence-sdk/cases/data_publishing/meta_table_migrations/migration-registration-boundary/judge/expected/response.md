The proposal splits one repository-owned schema change across incompatible
ownership paths. `Venue`, the changed bar table, and every related owned model
must be visible to the same provider registry so Alembic can reason about the
complete graph. Direct `Venue.register()` is not the normal path for a
platform-managed repository table and would leave migration history out of
sync.

Correct the SQLAlchemy models first, including the intended Venue key,
foreign-key target, nullability, indexes, and delete behavior. Add `Venue` to
the provider's registry. Inspect the provider's current revision, generate a
new revision, and review that it creates `Venue` before adding/replacing the
foreign key and that downgrade reverses the operations in a safe order. Check
revision ancestry and existing data implications before applying the upgrade.

If generation or upgrade fails, preserve the provider and database state.
Capture current revision/provider status, the exact command and traceback,
inspect whether any operation applied, correct the model or revision, and
resume from observed state. Do not blindly rerun and do not reset all
MetaTables. Completion requires an applied reviewed revision plus inspection
of both tables and their relationship; none of that execution is proven by the
prompt.
