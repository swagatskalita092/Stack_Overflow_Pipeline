# Reproducibility (Phase D)

Five consecutive end-to-end runs of the **same Python callables** the weekly
workflow uses (`scripts/run_cdn_e2e.py` → ingest, DQ, `dbt run`, `dbt test`,
`mark_candidate`, `publish_release`). Not Airflow: that is Part 1
(`docs/failure_modes.md`). Not the 13-row fixture: that is too small to
time.

Script: `scripts/chaos/measure_runtime.py`. Raw numbers:
`data/chaos/runtime_results.json`.

## The old ZIP URL 404s; ingest now uses GitHub

`https://survey.stackoverflow.co/datasets/stack-overflow-developer-survey-2024.zip`
returned **HTTP 404** on 2026-09-17. Follow-up: `ingest_survey.py` now
downloads the 2024 `results.csv` listed on survey.stackoverflow.co
([StackExchange/Survey `packages/archive/2024`](https://github.com/StackExchange/Survey/tree/main/packages/archive/2024)).
The weekly job uses that same module.

Timings below loaded the official 2024 public extract from Git LFS:

`https://media.githubusercontent.com/media/StackExchange/Survey/main/packages/archive/2024/results.csv`

- CSV: 159,525,875 bytes, header starts `ResponseId,MainBranch,Age,…`
- Rows after pandas: **65,437** (matches the 2024 methodology write-up)
- Wrapped once as a ZIP with inner name `survey_results_public.csv`
  (17,047,972 bytes) so `_extract_csv_from_zip` is unchanged
- First download of that CSV: **12.98 s** (recorded on the attempt that
  then died in dbt; the five timed runs reused the cached ZIP)

Each timed ingest therefore reads the local ZIP, not a live CDN. That is
honest: the live CDN path does not work.

## Machine

| | |
|---|---|
| Host | HP ENVY Laptop 14-eb0xxx |
| OS | Windows 10.0.26200 (platform string `Windows-10-10.0.26200-SP0`) |
| CPU | 11th Gen Intel Core i5-1135G7 @ 2.40 GHz, 4 cores / 8 threads |
| RAM | 16,917,712,896 bytes (~15.8 GiB) |
| Python | 3.11.9 (project `.venv`) |
| dbt | 1.7.0 + dbt-postgres 1.7.0 |
| Warehouse | `postgres:16` via Docker Compose on localhost:5432 |
| Airflow | stack running but DAG **paused** so it did not share the warehouse |

## Per-run times (seconds)

Ingest = unzip + pandas select/clean + `TRUNCATE`/`INSERT` 65,437 rows.
DQ = checksum of `raw.survey_responses` + six landing-table checks.
dbt artifacts were written to `data/chaos/dbt_target` (see caveats).

| run | ingest | DQ | dbt run | dbt test | total | published `release_id` |
|----:|-------:|---:|--------:|---------:|------:|------------------------|
| 1 | 10.42 | 0.97 | 17.06 | 9.89 | 38.53 | `0219829a-…` |
| 2 | 11.21 | 0.87 | 14.60 | 8.95 | 35.79 | `c66ce059-…` |
| 3 | 10.91 | 0.97 | 14.41 | 9.17 | 35.62 | `7cb63eda-…` |
| 4 | 11.11 | 1.04 | 15.05 | 9.33 | 36.69 | `44b04bd7-…` |
| 5 | 10.56 | 0.86 | 13.20 | 8.82 | 33.60 | `7e6f383b-…` |

## Median and range

| step | median | min | max |
|------|-------:|----:|----:|
| ingest | 10.91 | 10.42 | 11.21 |
| DQ + checksum | 0.97 | 0.86 | 1.04 |
| dbt run | 14.60 | 13.20 | 17.06 |
| dbt test | 9.17 | 8.82 | 9.89 |
| **total E2E** | **35.79** | **33.60** | **38.53** |

Range on total is 4.93 s (~14% of the median). Run 1 is the slowest
because dbt did a full parse (`saved manifest not found`); later runs
reused the host-only target dir.

After run 5, `dwh.active_release` is `7e6f383b-55d1-41bc-8faf-2fea83c0be7c`,
`raw.survey_responses` has 65,437 rows, and the live views are non-empty
(`marts.v_salary_analytics` 1,074 cells, `v_tech_adoption` 84,
`v_ai_sentiment` 3,828).

## What these numbers are not

- **Not a CDN download time.** Add the 12.98 s Git LFS fetch if you want
  "first cold extract." A working `survey.stackoverflow.co` ZIP would be
  a different size and a different number.
- **Not an Airflow DAG time.** Fixture DAG runs in Part 1 were ~40–80 s
  plus retries. A real-dataset DAG would also pay LocalExecutor startup
  and, on this laptop, the bind-mount dbt bug below.
- **Not a 114-column warehouse load.** Ingest keeps 23 modelled columns.
- **Not blocked by real-survey nulls.** DQ logged `null_country=6507` and
  `null_comp_total=31697` and continued. Those checks are log-only
  (`docs/data_quality_policy.md`). `total_rows_loaded=65437` does not
  block.

## Caveats that affected the clock

1. **Host dbt vs Compose bind mount.** The first timing attempt (after a
   successful 65,437-row ingest) died with
   `OSError: [Errno 22] Invalid argument` opening
   `dbt_project/target/graph_summary.json`: the same error overlapping
   Airflow `dbt_run_models` hit in injection 6. `dbt_project/` is mounted
   into the scheduler. The timing script then passed `--target-path
   data/chaos/dbt_target` so host dbt did not open the container's
   target files. Weekly CI is Ubuntu and does not use that flag.
2. **Marts are incremental aggregations**, so `dbt run` on 65k source
   rows still finishes in ~15 s. That is the real model; it is not a
   65k-row fact table rewrite.
3. Two leftover `building` releases (`dbff992e-…` from the 404 attempt,
   `77b35900-…` from the errno-22 attempt) were not published. They are
   not in `dwh.active_release`.
