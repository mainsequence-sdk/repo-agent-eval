Keep the owned producer as an updater instance and the external published input
as a read-only reference:

```python
class DailyRiskMetricConfig(TimeIndexTableUpdateConfig):
    pass


class DailyRiskMetricUpdater(TimeIndexTableUpdater):
    def __init__(
        self,
        *,
        config: DailyRiskMetricConfig,
        bars: DailyMarketBarUpdater,
        benchmark: TimeIndexTableRef,
        output_table: type[PlatformTimeIndexMetaTable] = DailyRiskMetricStorage,
        hash_namespace: str | None = None,
    ):
        self.config = config
        self.bars = bars
        self.benchmark = benchmark
        super().__init__(
            config=config,
            output_table=output_table,
            hash_namespace=hash_namespace,
        )

    def dependencies(self):
        return [self.bars, self.benchmark]
```

Only the `DailyMarketBarUpdater` dependency is executable and should be
scheduled before this updater. The `TimeIndexTableRef` contributes read-only
lineage and resolves an existing published table; it does not become a
producer, and this repository must not schedule it. Replacing the owned bars
producer with a ref would skip its required execution, while wrapping the
external table in a fake updater would claim ownership this repository lacks.

`output_table` is the explicit storage contract for the risk metric. It neither
makes the benchmark a producer nor transfers ownership of the referenced table
to this repository.
