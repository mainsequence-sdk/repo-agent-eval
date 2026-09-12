# Task

The tutorial describes two desired inputs:

- a row-oriented instrument master with stable `asset_identifier`, `symbol`,
  and `display_name` fields;
- daily market bars keyed by `time_index` and `asset_identifier`, with an
  adjusted-close column and daily cadence.

A user asks you to “discover both and then write the API that joins them.” No
live catalog output is provided. Explain how the discovery pass must classify
and identify the two surfaces, what evidence its report should contain, and
which work is outside this skill. Do not fabricate commands that have already
succeeded or implementation details not established by discovery.
