# Known limitations

Honest boundaries, not a backlog dressed up as documentation.

## Raw ingest is still truncate-and-reload

`raw.survey_responses` is **not** append-only. `scripts/ingest_survey.py`
truncates the table and bulk-inserts the current extract on every run.

That is a **deliberate Phase B scope boundary**. Publication safety is
guaranteed at the mart / `release_id` / `dwh.active_release` layer:

- a finished mart snapshot is tagged and kept
- readers use `marts.v_*` filtered to the active release
- a failed or superseded run cannot become the official answer

It is **not** guaranteed at the raw layer. If the loader dies after
`TRUNCATE` and before the insert commits, `raw.survey_responses` can be
empty (or half-loaded) until the next successful ingest. Staging and a
new candidate would then be built from that broken landing table — or
DQ would block on `total_rows_loaded = 0`, depending on when the crash
lands.

**Phase D** (failure-injection / chaos on the pipeline itself) should
specifically probe: kill ingest mid-truncate-and-reload, confirm what
raw, DQ, and the live views show, and decide whether raw needs its own
release/swap pattern. Phase B does not pretend that work is done.
