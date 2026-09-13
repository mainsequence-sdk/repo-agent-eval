Use platform-managed SQLAlchemy models with stable physical names and a shared
base:

```python
ACCOUNT_TABLE = schema_table_name("portfolio_console", "account")
POSITION_TABLE = schema_table_name("portfolio_console", "position")


class Account(PlatformManagedMetaTable, Base):
    __tablename__ = ACCOUNT_TABLE

    account_uid = mapped_column(Uuid, primary_key=True)
    display_label = mapped_column(String(255), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class Position(PlatformManagedMetaTable, Base):
    __tablename__ = POSITION_TABLE

    account_uid = mapped_column(
        Uuid,
        ForeignKey(f"{ACCOUNT_TABLE}.account_uid"),
        primary_key=True,
    )
    instrument_uid = mapped_column(Uuid, primary_key=True)
    quantity = mapped_column(Float, nullable=False)
```

The two-column primary key prevents duplicate positions for an account and
instrument. The foreign key establishes the account relationship. I would
delete positions in service code before deleting an account instead of defining
database cascade behavior. These models should be handled by the repository's
existing Alembic process.
