# Task

Design the row-oriented storage contract for a repository named
`portfolio_console`. It owns:

- `Account`: one row per broker account, stable key `account_uid`, display
  label, and creation timestamp;
- `Position`: one row per account/instrument pair, fields `account_uid`,
  `instrument_uid`, and `quantity`; positions must not outlive their account,
  and duplicate account/instrument rows are invalid.

Both tables are platform-managed and will evolve through the repository's one
MetaTable migration provider. Produce a concise SQLAlchemy-oriented design
sketch and explain physical naming, logical metadata, keys, relationship/delete
behavior, column descriptions, and provider scope. Do not run registration or
migrations.
