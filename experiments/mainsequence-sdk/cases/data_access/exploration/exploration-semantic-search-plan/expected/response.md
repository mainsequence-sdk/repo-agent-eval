This is natural-language discovery for published time-indexed data, so start
with semantic metadata search rather than list filters or column mode:

```bash
mainsequence time-index-table search "daily adjusted close for US exchange-traded funds"
```

Review the paginated candidates and retain, at minimum, each relevant
`TimeIndexMetaTable` UID, identifier, namespace, description, physical table
name, cadence, columns, and its data-source UID. If a
`time_indexed_profile.time_index_meta_table_uid` is present, confirm it matches
the parent table UID. Only after selecting a candidate should detail be used:

```bash
mainsequence time-index-table detail <TIME_INDEX_META_TABLE_UID>
```

The commands must run in the intended registered repository context because
the CLI derives the Organization Environment scope from its Git-resolved
CodeRepositoryBranch; do not supply or invent an Environment or branch UID.

Before execution, the only valid report is that this is the planned discovery
path and that no dataset identity has been verified. After execution, summarize
the exact candidates and evidence, unresolved grain/schema questions, and any
missing observation. Then hand the selected table identity and requirements to
`data_publishing/time_index_table_updates`; exploration does not design or
implement the volatility producer.
