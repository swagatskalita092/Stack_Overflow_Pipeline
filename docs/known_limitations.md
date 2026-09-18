# Known limitations

Honest boundaries, not a backlog dressed up as documentation.

## Raw ingest is year-scoped delete-and-reload, not append-only

`raw.survey_responses` is **not** append-only. `scripts/ingest_survey.py`
deletes `WHERE survey_year = :year` and bulk-inserts that year's extract.
A 2023 load does not destroy already-loaded 2024 rows (and the other way
around). A crash after the DELETE and before the INSERT commits still
rolls back with the current one-transaction boundary: the feared empty
slice is the year being loaded, not every year.

That is a **deliberate remaining gap at the raw layer**, now per year
instead of global truncate. Publication safety is guaranteed at the mart /
`release_id` / per-year `dwh.active_release` layer:

- a finished mart snapshot is tagged with `(release_id, survey_year)` and kept
- readers use `marts.v_*` joined to `dwh.active_release` on both keys
- a failed or superseded run cannot become that year's official answer
- publishing 2023 cannot move 2024's pointer

It is **not** guaranteed that a killed 2023 reload leaves 2023 raw intact.
Staging and a new 2023 candidate would then be built from that broken
slice, or DQ would block on `total_rows_loaded = 0` for that year.

**Phase D** probed the old whole-table TRUNCATE. The kill-after-delete
window still exists; the blast radius is one year.

## CI does not run the Airflow DAG

`.github/workflows/ci.yml` (required PR job) and
`.github/workflows/real-dataset-check.yml` (weekly CDN job) exercise the
pipeline **through the same Python modules and dbt CLI the DAG callables
use** (`scripts/release.py`, `scripts/ingest_survey.py`, `scripts/dq_checks.py`,
`dbt run` / `dbt test`). They do not start the Airflow scheduler, parse a
DagBag, or execute a DAG run.

What that means in practice:

- mart correctness, publication safety, and the real-dataset smoke test can
  all go green while Airflow task retries, pool slots, XCom, or a mis-wired
  `BashOperator` cwd still fail in a real scheduler
- Airflow **task order** is checked by the AST walk in
  `tests/test_dag_structure.py` (and the string check in
  `tests/test_publication_safety.py`), not by an automated end-to-end DAG
  execution
- Airflow **retries, scheduling (`@weekly`), and operator runtime** are
  covered by manual Compose runs only

That is a deliberate Phase C scope boundary: PR CI stays off the survey CDN
and off a multi-minute Airflow image boot. It is not a claim that the
scheduler was tested.

## USD conversion covers ~31 currencies, not every currency in the survey

`comp_total_usd_converted` and the `*_salary_usd` mart columns use real
European Central Bank reference rates (via the Frankfurter API), which
covers the 30 non-EUR currencies the ECB publishes daily plus EUR and USD
themselves — see `seeds/fx_rates_to_usd.csv`. A respondent whose currency is
not one of those (the ECB does not publish daily rates for every world
currency — for example several are excluded from official ECB publication
for sanctions or data-availability reasons) gets `comp_total_usd_converted
= NULL`. That respondent still counts in `respondent_count` and in the raw
`avg_salary` / `median_salary` / etc.; they are simply excluded from the
USD aggregates and from `fx_converted_count`. This is a deliberate honesty
boundary: an unmapped currency is never assigned a guessed or default rate.

Each year's rate is also a **single fixed date**, not the date the
individual respondent completed the survey (Stack Overflow's own published
methodology takes the same shortcut for 2024). A currency that moved
significantly against the dollar during the ~2-3 month survey collection
window will have some genuine within-year drift baked into every
`*_salary_usd` figure for that currency. That is accepted, not corrected,
for the same reason Stack Overflow accepts it: doing per-respondent-date FX
would require knowing each respondent's actual completion date to the day,
which the public extract does not reliably provide.
