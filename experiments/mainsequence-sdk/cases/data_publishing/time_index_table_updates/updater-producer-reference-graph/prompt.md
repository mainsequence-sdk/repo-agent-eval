# Task

Design the dependency boundary for a `DailyRiskMetricUpdater`. It computes
daily risk from:

1. `DailyMarketBarUpdater`, owned by this repository and expected to execute
   before the risk calculation;
2. an organization-published benchmark-return table owned elsewhere, which
   this repository may read but must never schedule or update.

A teammate proposes returning two `TimeIndexTableRef` objects from
`dependencies()` “because both inputs are tables.” Give the correct
constructor/dependency sketch and explain execution, lineage, and storage ownership
behavior. The output is a separate `DailyRiskMetricStorage` table. Do not claim
the graph was executed.
