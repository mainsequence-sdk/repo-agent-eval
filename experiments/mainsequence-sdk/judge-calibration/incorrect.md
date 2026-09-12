Use platform-managed row models, with their contract hashes serving as the
physical and logical identities:

```python
class Account(PlatformManagedMetaTable, Base):
    __tablename__ = compute_metatable_contract_hash(columns=["account_uid"])

    account_uid = mapped_column(Uuid, primary_key=True)
    display_label = mapped_column(String)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class Position(PlatformManagedMetaTable, Base):
    __tablename__ = compute_metatable_contract_hash(
        columns=["account_uid", "instrument_uid", "quantity"]
    )

    account_uid = mapped_column(Uuid, primary_key=True)
    instrument_uid = mapped_column(Uuid, primary_key=True)
    quantity = mapped_column(Float)
```

Call `Account.register()` and `Position.register()` at application startup.
There is no need for a foreign key because the account identifier is already
stored on each position, and migrations are unnecessary when registration runs
on every startup.
