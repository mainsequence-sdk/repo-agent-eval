Use `PlatformManagedMetaTable` models on one shared declarative `Base`. Derive
explicit repository-prefixed physical names with `schema_table_name`, for
example:

```python
ACCOUNT_TABLE = schema_table_name("portfolio_console", "account")
POSITION_TABLE = schema_table_name("portfolio_console", "position")

class Account(PlatformManagedMetaTable, Base):
    __tablename__ = ACCOUNT_TABLE
    __metatable_identifier__ = "portfolio_console.Account"
    __metatable_namespace__ = "portfolio_console"
    __metatable_description__ = "One row per broker account used by portfolio positions."
    account_uid = mapped_column(String, primary_key=True, info={
        "label": "Account UID", "description": "Stable broker-account identifier."
    })
    # display_label and created_at also receive intention-rich label/description info

class Position(PlatformManagedMetaTable, Base):
    __tablename__ = POSITION_TABLE
    __metatable_identifier__ = "portfolio_console.Position"
    __metatable_namespace__ = "portfolio_console"
    __metatable_description__ = "One row per account and instrument pair."
    account_uid = mapped_column(
        ForeignKey(f"{ACCOUNT_TABLE}.account_uid", ondelete="CASCADE"),
        primary_key=True,
        info={"label": "Account UID", "description": "Owning broker account."},
    )
    instrument_uid = mapped_column(String, primary_key=True, info={
        "label": "Instrument UID", "description": "Instrument held in the account."
    })
    # quantity also has label and business description metadata
```

The composite primary key enforces the declared Position grain. The foreign
key establishes the relationship, and `CASCADE` matches the explicit rule that
positions cannot outlive the account; it should not be chosen without that
lifecycle requirement. Every mapped column, including omitted sketch fields,
needs `info.label` and an intention-rich `info.description`.

Include both models and the shared metadata in the same migration provider so
Alembic creates/evolves the relationship. Do not call `.register()` from
application code. The migration skill owns generation, review, and upgrade.
