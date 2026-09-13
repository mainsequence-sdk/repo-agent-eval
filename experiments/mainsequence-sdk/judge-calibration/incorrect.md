Use platform-managed row models with stable physical names:

```python
ACCOUNT_TABLE = schema_table_name("portfolio_console", "account")
POSITION_TABLE = schema_table_name("portfolio_console", "position")


class Account(PlatformManagedMetaTable, Base):
    __tablename__ = ACCOUNT_TABLE

    account_uid = mapped_column(Uuid, primary_key=True)
    display_label = mapped_column(String)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class Position(PlatformManagedMetaTable, Base):
    __tablename__ = POSITION_TABLE

    account_uid = mapped_column(Uuid, primary_key=True)
    instrument_uid = mapped_column(Uuid, primary_key=True)
    quantity = mapped_column(Float)
```

Call `Account.register()` and `Position.register()` at application startup.
There is no need for a foreign key because the account identifier is already
stored on each position, and migrations are unnecessary when registration runs
on every startup.
