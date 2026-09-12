Use stable names and a cascading account foreign key:

```python
ACCOUNT_TABLE = schema_table_name("portfolio_console", "account")
POSITION_TABLE = schema_table_name("portfolio_console", "position")


class Account(PlatformManagedMetaTable, Base):
    __tablename__ = ACCOUNT_TABLE
    account_uid = mapped_column(Uuid, primary_key=True)


class Position(PlatformManagedMetaTable, Base):
    __tablename__ = POSITION_TABLE
    account_uid = mapped_column(
        Uuid,
        ForeignKey(f"{ACCOUNT_TABLE}.account_uid", ondelete="CASCADE"),
        primary_key=True,
    )
    instrument_uid = mapped_column(Uuid, nullable=True)
    quantity = mapped_column(Float, nullable=False)
```

The model above enforces one row per account and instrument because
`instrument_uid` is part of the composite primary key. Deleting an account must
never delete its positions, so the foreign key should remain `CASCADE` and the
database will restrict the deletion. Both tables belong to one migration graph,
but they should be placed in separate providers so each can evolve independently.
