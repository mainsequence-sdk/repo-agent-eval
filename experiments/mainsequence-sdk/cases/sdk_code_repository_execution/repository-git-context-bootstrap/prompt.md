# Task

A user wants to modify a repository's daily-price updater and then verify its
published output on the platform. They have not defined success. The checkout
is on local Git branch `feature/prices`; authentication works, but nobody has
verified whether that branch is registered. A teammate proposes asking for an
Environment UID and saving it in `.env` so work can begin.

Write the correct bootstrap and routing plan. Separate local work from live
branch-owned work, define an observable success condition, identify the exact
context check, and route domain implementation. Do not implement the updater or
claim platform context is resolved.
