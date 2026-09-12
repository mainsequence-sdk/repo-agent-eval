Every persisted Job uses one exact image; there is no blank, dynamic “latest,”
or branch-tip execution mode.

`backfill-once` is manual/pinned, so its creation must include
`--related-image-uid image-202` and explicitly keep automatic deployment off.
Confirm that image is ready and represents the intended pushed commit.

`nightly-refresh` should use `--automatic-deployment` and must omit
`--related-image-uid`. For automatic creation, the backend derives the exact
initial image from the CodeRepositoryBranch's persisted synchronized commit;
future qualifying immutable repository events may atomically promote another
exact image. The caller never combines automatic mode with an image selection,
and automatic mode still does not deploy unpushed local changes.

Creation proves only that the Job exists. When execution success matters,
inspect the created Job and its exact image, trigger the manual run or observe a
scheduler-triggered run, list JobRuns, and inspect the terminal run status and
owner-scoped logs. For the automatically deployed Job, also inspect relevant
deployment/run evidence for the expected synchronized commit and selected exact
image. None of these checks is claimed complete here.
