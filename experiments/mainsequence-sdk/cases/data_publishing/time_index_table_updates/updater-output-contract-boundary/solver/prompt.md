# Task

Review this updater design:

```python
class DailyCloseConfig(TimeIndexTableUpdateConfig):
    symbols: ClassVar[tuple[str, ...]] = ("AAPL", "MSFT")
    source_name: str = Field(default="vendor-a", exclude=True)


class DailyCloseUpdater(TimeIndexTableUpdater):
    def __init__(self, config: DailyCloseConfig):
        self.config = config
        super().__init__(config=config)

    def update(self):
        frame = fetch(self.config.symbols, source=self.config.source_name)
        frame.index = pd.Index([date.today()] * len(frame), name="date")
        return frame
```

`symbols` and `source_name` change the produced data. The declared storage
expects a two-level index named `time_index` and `asset_identifier`, with
`time_index` exactly `datetime64[ns, UTC]`. Produce a corrected design sketch
and explain output storage, configuration hashing, validation isolation, and
incremental output behavior. Do not run it.
