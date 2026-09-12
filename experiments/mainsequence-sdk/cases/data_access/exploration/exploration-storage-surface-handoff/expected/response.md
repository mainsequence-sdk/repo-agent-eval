Treat the two objects separately. The instrument master is a row-oriented
`MetaTable`; the daily bars are a published `TimeIndexMetaTable`. For the bars,
use time-index-table semantic search because only a description is known, then
inspect candidate detail after selecting it. For the instrument master, inspect
registered MetaTable objects through the SDK client rather than pretending it
is a time-index table.

The discovery report should record the exact object kind, identifier,
public/storage UID, namespace, description, physical name when exposed,
relevant columns and keys, cadence for the bars, and the concrete observation
that produced each value. It should explicitly say “not found” or “not yet
verified” when no observation establishes an identity. It must not infer that
similar column names prove compatible grain, keys, or join semantics.

The output is a discovery handoff: candidate identities plus unresolved
questions about key compatibility and the desired join. MetaTable design or
registration belongs to `data_publishing/meta_tables`; a new/changed producer
belongs to `data_publishing/time_index_table_updates`; the FastAPI and Command
Center contract work belongs to `application_surfaces/api_surfaces`. This pass
does not write the API or claim the join works.
