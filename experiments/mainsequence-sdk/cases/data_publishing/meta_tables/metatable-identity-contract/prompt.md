# Task

Review this proposed platform-managed row table:

```python
class WatchlistItem(PlatformManagedMetaTable, Base):
    __tablename__ = compute_metatable_contract_hash(columns=["user", "symbol"])
    __metatable_identifier__ = __tablename__
    user = mapped_column(String, primary_key=True)
    symbol = mapped_column(String, primary_key=True)
    note = mapped_column(String, nullable=True)
```

The repository is `research_portal`; the intended grain is one row per human
user and symbol. Explain the identity and contract problems and show the key
metadata corrections. The schema will be managed through an existing Alembic
MetaTable provider. Do not implement or apply a migration.
