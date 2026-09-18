# Failure modes (Phase D)

Live injections against the real Docker Compose stack
(`postgres:16` + `apache/airflow:2.8.1` LocalExecutor). Each row is one
script under `scripts/chaos/`, run by hand, not CI.

Part 1 uses the Phase A fixture ZIP (~13 rows) through a real DAG run so
we are testing Airflow + publication, not the CDN. Part 2 timings against
the public 2024 extract live in `docs/reproducibility.md`.

Nothing here was patched after the fact. Where the feared bug did not
show up, the table says so. Where a different bug showed up, the table
says that too.

## How to read the table

- **Recovered?** Did the pipeline reach a terminal DAG state and leave
  the warehouse reachable, without an operator rebuilding it by hand?
- **Correctness held?** After the dust settled, did `marts.v_*` still
  show exactly one published `release_id`, and did a bad/partial run
  stay off the live pointer?

Reusable scripts: `scripts/chaos/01_malformed_source.py` …
`06_overlapping_dag_runs.py`, plus `00_baseline_publish.py`. Shared
helpers are in `scripts/chaos/_common.py`.

Baseline before injection 1: DAG run `chaos_00_baseline_fixture`
published `f40dbf53-…` (seq 2), `raw_n=13`, views singleton. Seq 1 is a
leftover scheduled run that opened a release then failed; it never
became active.

| # | Injected condition | What happened | Recovered? | Correctness held? |
|---|--------------------|---------------|------------|-------------------|
| 1 | Truncated CSV inside the survey ZIP (last row cut in half) before `ingest_raw_survey`. Script: `01_malformed_source.py`. | pandas loaded the broken last row anyway. DQ: `null_country=1` (log-only), `null_comp_total=2` (log-only), `total_rows_loaded=13` (does not block unless `n==0`). Every DAG task succeeded. Active moved `f40dbf53-…` → `385fa4e8-…` (seq 3). | Yes. DAG `success`. | **No.** Junk source became the live release. Truncation is not a parse error, and landing-table DQ does not treat a partial row as a blocker. |
| 2 | SIGKILL the loader after `TRUNCATE raw.survey_responses`, before `INSERT` commits. Phase B called this an open raw-layer gap. Script: `02_kill_loader_mid_truncate.py` (pidfile + 25s sleep after TRUNCATE). | Kill landed (`KILLED 813`). Mid-kill snapshot: `raw_n` still 13, active unchanged: the one-transaction TRUNCATE+INSERT rolled back. Airflow retried ingest after `retry_delay=5 minutes` and published seq 6 (`a336695c-…`). | Yes, after the 5-minute retry. DAG `success`. | **Yes, for this ingest.** The feared empty raw table did **not** appear. Views stayed on the previous release until the retry published a full load. This does **not** prove raw is append-only; it proves the current transaction boundary survived SIGKILL. |
| 3 | `docker compose kill postgres` while `dbt_run_models` was running. Script: `03_kill_postgres_mid_dbt.py`. | Warehouse and Airflow metadata share the same Postgres. The scheduler container died with the database. The chaos script timed out at 300s (`TimeoutError`) because there was no scheduler to finish the DAG. Views stayed on seq 6 during downtime. After `docker compose up -d airflow-scheduler`, the same run (`chaos_03_kill_postgres_mid_dbt`) **re-ran `dbt_run_models` to completion** (`try_number=1`, full incremental DELETE-this-`release_id` + INSERT) and published seq 7 (`41c78bef-…`). That mart is a **complete fixture rebuild**, not a half-written table: see the comparison below. | Partially. Postgres came back; the scheduler did not, until an operator recreated it. Once recreated, the DAG finished a clean dbt run and published. | **Yes for the data.** `marts.mart_salary_analytics` for `41c78bef-…` is byte-for-byte the same one-row cell as the clean fixture publish immediately before (seq 6 `a336695c-…`). **Operational remaining gap:** an operator must recreate the scheduler, and Airflow then auto-completes/publishes instead of staying failed. That is not a partial-data publish. |
| 4 | A dbt test that always fails, added for one DAG run (`dbt_project/tests/chaos_phase_d_always_fail.sql`: `select 1 where 1=1`). Script: `04_downstream_test_failure.py`. Not pytest. | `dbt_test_models=failed`. `mark_candidate` and `publish_release` were `upstream_failed`. Active stayed `41c78bef-…`. Candidate seq 40 marked `failed`. | Yes. DAG `failed` as designed. Test file removed in `finally`. | **Yes.** Previous published release stayed live. A failing downstream test cannot move `dwh.active_release`. |
| 5 | SIGKILL `publish_release` after the publish transaction commits, before Airflow marks the task successful. Script: `05_kill_after_publish.py` (pidfile + 25s sleep after commit, DAG `retry_delay` patched to 15s). | Pidfile appeared (`pidfile=872`) but `os.kill` raised `ProcessLookupError: [Errno 3] No such process`. The task had already exited. DAG `success` on `try_number=1`. Active moved to `e40bb43f-…` (seq 41). `already_active` was **not** exercised. | Yes, because the kill missed. | **Inconclusive for the crash-after-commit path.** Publication itself looked fine (singleton view, pointer moved once). The retry/`already_active` contract was not hit. See gaps. |
| 6 | Two real DAG runs overlapping. Temporarily `max_active_runs=2`, 20s ingest sleep, triggers 8s apart. Script: `06_overlapping_dag_runs.py`. | Both runs were `running` at once. Ingest windows overlapped (`chaos_06_overlap_a` 23:01:25–23:01:46 UTC, `b` 23:01:39–23:02:06). Concurrent `dbt run` on the bind-mounted `dbt_project/target/` hit `OSError: [Errno 22] Invalid argument` writing `graph_summary.json`. Both `dbt_run_models` went `up_for_retry`. Attempt 2: `a` succeeded (`try_number=2`), `b` failed on the same errno writing `mart_tech_adoption.sql`. `a` published seq 42 (`205dd0a1-…`). `b` seq 43 `failed`, publish `upstream_failed`. Views singleton on 42. No `superseded` row: `b` never reached publish, so the monotonicity guard was not the thing that saved us. | Yes. One DAG `success`, one `failed`. | **Mostly yes at the pointer.** Readers saw one published id, never a mix, never a rollback to an older seq. **No at the engine:** overlapping `dbt run` is unsafe on this Windows bind-mount, and overlapping ingest is two TRUNCATE+INSERT on the same raw table. |

### Injection 3: was the published mart complete?

Yes. After the scheduler was recreated, Airflow ran a **fresh, complete**
`dbt_run_models` for release `41c78bef-ec1e-5f9d-a7fd-f556c609c0ed` (the
incremental pre-hook deletes that `release_id` then inserts). It was not a
resume of a half-written table.

`marts.mart_salary_analytics` for that id, compared to the clean fixture
DAG immediately before (seq 6 `a336695c-…`) and the Phase D baseline
(seq 2 `f40dbf53-…`):

| release | `respondent_count` | avg | median | p25 | p75 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| seq 2 baseline | 7 | 249999.86 | 130000 | 115000 | 145000 | 100000 | 999999 |
| seq 6 clean (after inj 2) | 7 | 249999.86 | 130000 | 115000 | 145000 | 100000 | 999999 |
| seq 7 **after inj 3** | 7 | 249999.86 | 130000 | 115000 | 145000 | 100000 | 999999 |

Same grain (`United States` / `5-9 years` / `Developer, full-stack` /
`Remote` / `100 to 499 employees`). AI mart: 2 rows on both seq 6 and
seq 7. Tech mart: 0 rows (fixture is under the ≥100 cutoff).

Those numbers are **not** the Phase A hand-calc (`n=6`, max `150000`)
because the chaos ZIP omits `loaded_at`, so staging cannot drop R008's
old `999999` row. That is the harness, and it is identical on the clean
runs. Injection 3 did not add extra rows, drop a cell, or leave a null
`respondent_count`.

## Gaps (not silently fixed)

1. **A last-row-only truncate still has the same row count.** pandas
   loaded the cut line; `null_country` stayed log-only. Follow-up:
   `row_count_drop` now blocks when the load is under 50% of the last
   published `total_rows_loaded` (`docs/data_quality_policy.md`). That
   would **not** have stopped injection 1's exact cut (still 13 rows).
   It does stop a truncated file that loses most rows
   (`tests/test_dq_row_count_drop.py`).

2. **Raw truncate-and-reload is still one table, one transaction.**
   SIGKILL during the sleep-after-TRUNCATE rolled back (good). Two
   overlapping DAG runs can still TRUNCATE the same table at the same
   time (injection 6). Staging a swap table was not added.

3. **Airflow metadata and the warehouse share one Postgres.** Killing
   the warehouse kills the scheduler. Compose does not restart
   `airflow-scheduler` when Postgres comes back. An operator had to
   `docker compose up -d airflow-scheduler`. After that the DAG finished
   a **complete** dbt rebuild and published (see injection 3 table). The
   remaining issue is shared-DB / manual scheduler recreate, not a
   partial mart becoming live.

4. **Default `retry_delay` is 5 minutes.** Injection 2 looked hung until
   the retry fired. Injection 6's concurrent dbt failures each waited
   five minutes before attempt 2.

5. **LocalExecutor workers do not put `task_id` in the process command
   line**, and the Airflow image has no `ps`. Chaos had to write a
   pidfile from inside the task. That is a test-harness limit, not a
   warehouse bug, but it is why injection 5 raced: by the time the host
   read `killme.pid`, pid 872 was already gone.

6. **Crash-after-publish (`already_active`) was not observed.** The 25s
   post-commit sleep was not a wide enough window once
   `wait_until_running` + pidfile poll returned. We did not re-run it
   to force a hit; the honest result is a missed kill.

7. **Overlapping `dbt run` on a Docker Desktop bind mount.** Both runs
   write `dbt_project/target/` on the Windows host. `OSError: [Errno 22]
   Invalid argument` took down `dbt_run_models` for both on attempt 1
   and for run `b` on attempt 2. Publication safety still held because
   the loser never reached `publish_release`. That is luck of which run
   won the file, not a lock around dbt.

8. **`seq` jumped 7 → 40 after the Postgres kill.** That is expected
   PostgreSQL sequence behavior (`BIGSERIAL` / `nextval` values are not
   rolled back when a transaction aborts), not missing release rows or
   data loss. The pointer still moved only forward to a published
   candidate.

9. **Historical rows stay `published`.** `superseded` is only applied to
   a *late* candidate whose `seq` is not newer than the current active
   seq (`scripts/release.py`). Older successful publishes are not
   rewritten. That matches the code. Injection 6 never produced a
   `superseded` row because the slower run failed before publish.

10. **The old ZIP URL 404s; ingest now uses the GitHub archive.**
    `https://survey.stackoverflow.co/datasets/stack-overflow-developer-survey-2024.zip`
    is still 404. Follow-up: `SURVEY_DATA_URL` in `scripts/ingest_survey.py`
    is the 2024 `results.csv` from
    [StackExchange/Survey](https://github.com/StackExchange/Survey/tree/main/packages/archive/2024)
    (the link survey.stackoverflow.co lists today). ZIP bytes are still
    accepted if that host ever returns one.

## Harness notes (not pipeline patches)

- Chaos patches bind-mounted `scripts/ingest_survey.py`,
  `airflow/dags/stackoverflow_pipeline_dag.py`, and (injection 5)
  `scripts/release.py`, then restores the original bytes in `finally`.
  After the suite, those files had no leftover `CHAOS` strings.
- `docker-compose.yml` sets `AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL=15`
  and `MIN_FILE_PROCESS_INTERVAL=10` so a DAG-file patch is picked up
  in seconds instead of the Airflow default (~5 minutes). That is so
  the injections can run; it does not change publication behaviour.
- The Airflow image already had `protobuf 4.25.2` (dbt-postgres 1.7
  teardowns crash on protobuf 5). No extra pip inside the scheduler
  was required for these runs.

## What we did not claim

- These six runs are not a proof that raw ingest is crash-safe for a
  65k-row CDN load. The truncate-kill used the 13-row fixture and a
  25s artificial sleep.
- CI still does not start Airflow. This document is the Airflow
  coverage; `.github/workflows/ci.yml` is unchanged.
- Injection 5 does not prove `already_active`. It proves the kill
  missed.
