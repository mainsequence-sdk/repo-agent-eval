Make all output-affecting inputs explicit hashed configuration, keep true class
invariants as `ClassVar`, and pass the storage contract to the base class:

```python
class DailyCloseConfig(TimeIndexTableUpdateConfig):
    symbols: tuple[str, ...]
    source_name: str = "vendor-a"

class DailyCloseUpdater(TimeIndexTableUpdater):
    time_index_name: ClassVar[str] = "time_index"

    def __init__(
        self,
        *,
        config: DailyCloseConfig,
        output_table: type[PlatformTimeIndexMetaTable] = DailyCloseStorage,
        hash_namespace: str | None = None,
    ):
        super().__init__(
            config=config,
            output_table=output_table,
            hash_namespace=hash_namespace,
        )
```

Use an explicit `hash_namespace` only to isolate an intentional first validation against shared
backend state, not to hide meaningful configuration differences. Do not mark
`symbols` or `source_name` hash-excluded because they affect results.

The update calculates its requested incremental window from update statistics,
fetches only that window (including any explicit warm-up), and returns an
empty-but-valid frame when no new rows exist. Any non-empty result must be
indexed first by `time_index` with exact `datetime64[ns, UTC]`, then by
`asset_identifier`, sorted and deduplicated at that grain. A Python `date`
index named `date` violates the storage contract. Tail cleanup, if genuinely
required, must use `TimeIndexMetaTable.delete_after_date(...)` with a bounded
scope; it is not a generic rewrite mechanism.
