The contract hash is not a stable logical or physical table identity. It may be
used as optional contract-version metadata, but changing columns must not
silently rename the table or its catalog identifier. Use a stable,
repository-prefixed physical name such as:

```python
WATCHLIST_ITEM_TABLE = schema_table_name("research_portal", "watchlist_item")

class WatchlistItem(PlatformManagedMetaTable, Base):
    __tablename__ = WATCHLIST_ITEM_TABLE
    __metatable_identifier__ = "research_portal.WatchlistItem"
    __metatable_namespace__ = "research_portal"
    __metatable_description__ = "One row per human user and watched market symbol."

    user_uid = mapped_column(String, primary_key=True, info={
        "label": "User UID", "description": "Canonical public UID of the watchlist owner."
    })
    symbol = mapped_column(String, primary_key=True, info={
        "label": "Market Symbol", "description": "Instrument symbol unique within the user's watchlist."
    })
    note = mapped_column(String, nullable=True, info={
        "label": "Note", "description": "Optional user-authored annotation for this item."
    })
```

The composite key matches the stated row grain. Every mapped column needs both
a label and an intention-rich description; the table description must explain
business purpose and grain. Keep the authored table name stable so catalog
sync resolves the existing table correctly. Send the changed model through the
selected provider's Alembic revision/review/upgrade lifecycle rather than
direct registration or runtime DDL.
