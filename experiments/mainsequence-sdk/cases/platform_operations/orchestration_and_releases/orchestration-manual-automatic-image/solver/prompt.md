# Task

Review two standalone Job proposals for pushed repository code:

1. `backfill-once` must remain pinned to already-ready image UID `image-202`.
   The proposal omits an image and says the Job will use “latest.”
2. `nightly-refresh` should follow future qualifying synchronized commits with
   automatic deployment. The proposal passes both `--automatic-deployment` and
   `--related-image-uid image-202`.

Explain the correct image configuration for each Job, what exactness means,
and the minimum post-creation evidence needed before reporting execution
success. Do not create or run either Job.
