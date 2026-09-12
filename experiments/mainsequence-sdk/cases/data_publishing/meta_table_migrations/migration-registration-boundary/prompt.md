# Task

A repository already manages `Asset` and `DailyMarketBarStorage` through one
Alembic MetaTable provider. `DailyMarketBarStorage.asset_identifier` has a
foreign key to `Asset.asset_identifier`. A proposed change replaces that
foreign key and adds a new repository-owned `Venue` table, but the author:

- leaves `Venue` out of the provider registry;
- invokes `Venue.register()` from a seed script;
- generates a revision containing only the bar-table change;
- proposes a “reset all MetaTables” command if upgrade fails.

Review the proposal and give a safe correction sequence. Focus on ownership,
the complete model graph, revision review, and failure recovery. Do not execute
the migration.
