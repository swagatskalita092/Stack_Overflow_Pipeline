# Stack Overflow Developer Survey: Project Summary Report

## 1. What the Project Is

An end-to-end data engineering pipeline for the Stack Overflow Developer Survey, covering two real years, 2023 and 2024. It ingests the official public survey extracts (65,437 rows, 114 columns for 2024; 89,184 rows, 84 columns for 2023), loads them into a PostgreSQL warehouse, runs automated data quality checks, and transforms the data through a layered dbt model into three analytical marts:

- **Salary analytics**: compensation benchmarks by country, experience band, role, remote work, and organization size
- **Technology adoption**: usage and "want to continue" retention for programming languages and databases
- **AI sentiment**: how developers feel about AI tools, by country and role, with job satisfaction and AI-threat perception where the survey year actually asked

The pipeline is orchestrated by Apache Airflow on a weekly schedule, runs in Docker Compose (Postgres and Airflow), and publishes a static dashboard from the real published data after every run.

That description alone makes this sound like a standard ingest-transform-publish pipeline, and the first version of it was. What this report actually documents is what was added on top, deliberately, to answer a harder question than "does it run": can this pipeline be trusted? Can a bad or partial run ever silently become the official published answer? Can it survive its data source actually changing shape, not just a bigger version of the same shape? And when something is wrong, does the project find that out through disciplined verification, or does it just look finished?

## 2. Why It Was Built This Way

The original pipeline (ingest, DQ checks, dbt run, dbt test, one survey year, `TRUNCATE`-and-reload) was functional but made no promises about correctness under real conditions: a partial ingest could publish, a failed run could leave the warehouse in an ambiguous state, and there was no evidence the published numbers actually matched what the raw data said. Two directions were considered for hardening it: bolt on more infrastructure (Kafka, a lakehouse, a cloud warehouse), or prove the existing stack is actually trustworthy. The second was chosen deliberately. More infrastructure for an annual, mostly-static dataset would be manufactured complexity, not real engineering signal. The actual gaps were correctness guarantees and evolvability, not throughput or scale.

Everything below follows from that decision: six phases, each closing one specific, real gap, each verified independently rather than taken on the strength of a passing test suite alone.

## 3. Architecture and Approach

### 3.1 High-level flow

1. **Ingest.** A Python script downloads that year's survey extract, cleans it (column selection, renames, sentinel handling), and loads it into `raw.survey_responses`, scoped to that year (`DELETE WHERE survey_year = :year` then insert, not a blanket truncate, so ingesting one year never destroys another year's already-loaded data).
2. **Data quality.** Checks run against the raw table (null/duplicate keys, null country/comp, invalid experience fields, a row-count-drop check against the last published release for that year) and log to `dwh.dq_issues`. A catastrophic truncation blocks publication; ordinary monitoring checks don't.
3. **Transform.** dbt runs three layers: staging (dedupe, type casting, cleaning; grain is `(survey_year, response_id)`), intermediate (exploded language/database arrays), then marts (salary, tech adoption, AI sentiment), every row tagged with both `release_id` and `survey_year`.
4. **Test.** dbt schema tests run on the models; a separate correctness test independently confirms the published marts match hand-calculated expected values for known fixture data, not just "the models built without error."
5. **Publish.** A candidate is only promoted to the live, reader-visible pointer after tests pass, inside one locked transaction, scoped to that specific survey year.
6. **Dashboard.** After a successful publish, a static page is regenerated from the real published data. A rendering failure here cannot touch the release that was just published.

### 3.2 Technology choices

| Component | Choice | Rationale |
| --- | --- | --- |
| Orchestration | Apache Airflow 2.8 | DAG-based scheduling, retries, clear task dependencies |
| Warehouse | PostgreSQL 16 | Single DB for raw, dwh, and marts; advisory locks make the publication-safety pattern possible without extra infrastructure |
| Transformations | dbt (dbt-postgres 1.7) | Layered models, schema tests, SQL-first transformations |
| Ingestion / DQ | Python (pandas, psycopg2, requests) | Flexible cleaning and bulk insert; easy to extend checks |
| Environment | Docker Compose | One-command bring-up for Postgres and Airflow with shared volumes |
| CI | GitHub Actions, `postgres:16` service container | Real dbt against real fixtures on every PR, without depending on the survey CDN or a multi-minute Airflow boot |

### 3.3 The publication-safety mechanism

This is the part of the project that answers "can a bad run silently become the official answer." Marts are append-only, every row tagged with a `release_id` and a `survey_year`. `dwh.pipeline_releases` records one row per attempted run (a monotonic `seq`, a source checksum, a status). `dwh.active_release` holds one live pointer per survey year, not one pointer for the whole warehouse, so 2023 and 2024 publish and fail independently and can never move each other's pointer. Readers query `marts.v_*` views, which join to the active release on `(survey_year, release_id)`; they never see a mix of two runs, and a failed or superseded run never becomes visible.

`publish_release` flips a year's pointer in one Postgres-advisory-locked transaction, comparing the candidate's `seq` only against that same year's currently active release, not against a global sequence. That distinction matters because `seq` is shared across all years: a 2023 release built after several 2024 releases has a numerically higher seq than 2024's active release, so a global compare would have let an unrelated year's release spuriously roll back or supersede another year's publish. This exact failure mode was caught in design review before it was implemented, not found afterward.

### 3.4 Orchestration (Airflow DAG)

- **DAG ID:** `stackoverflow_survey_pipeline`
- **Schedule:** `@weekly`, or triggered manually with `{"survey_year": 2023}` (or 2024) in the run config
- **Tasks, in order:** `open_release`, `ingest_raw_survey`, `record_source_checksum`, `run_dq_checks`, `dbt_run_models`, `dbt_test_models`, `mark_candidate`, `publish_release`, `render_dashboard`

## 4. The Dataset

- **Source:** Stack Overflow Developer Survey, official public extracts for 2023 and 2024 (survey.stackoverflow.co, mirrored via `github.com/StackExchange/Survey`).
- **2024:** 65,437 real rows, 114 columns, confirmed via a live ingest.
- **2023:** 89,184 real rows, 84 columns, confirmed via a live ingest.
- **Columns actually used:** 23 renamed source columns spanning identity, demographics, experience, work, compensation, and AI fields. Multi-select fields (languages, databases, platforms) are semicolon-separated in the source, kept as text through raw/staging, and exploded in the intermediate layer.
- **Schema drift between years, verified directly, not assumed:** comparing the pipeline's 23 used columns against the real 2023 and 2024 headers found 21 match exactly by name in both years; exactly 2 (`AIThreat`, `JobSat`) are genuinely absent from 2023. This is the concrete substance behind "the pipeline survives its data source changing shape." It isn't a hypothetical, it's a real, documented, tested difference between two real datasets.

## 5. What Each Phase Actually Closed

The plan was six phases, each closing one specific gap, in order of signal-per-effort. Every phase was independently verified in this project's working sessions: a fresh clone, a real re-run of the test suite against a live Postgres container, and, once CI existed, a direct fetch of the live GitHub Actions run rather than trusting a self-report.

**Phase A, semantics, fixtures, and a real quality policy.** Established what each mart column actually means, with a dozen hand-calculated fixture respondents and independently-computed expected outputs. Investigated a suspected fan-out bug in the salary mart and found none, a correctly negative result, not a wasted one. Found and fixed a real bug: a field named `comp_total_usd` never actually converted anything to USD; renamed to `comp_total_raw` and documented honestly against Stack Overflow's own conversion methodology.

**Phase B, safe, replayable publication.** Built the release/candidate/publish pattern described in section 3.3. Proved four core safety properties with regression tests (identical rerun never doubles what readers see, a failed test leaves the previous release live, a simulated crash-then-retry publishes exactly once, overlapping publishes serialize without corruption). Caught and fixed a real race: two different candidates publishing concurrently could resolve as last-write-wins, letting a stale run overwrite a newer one; fixed with a monotonicity guard.

**Phase C, CI that actually exercises the pipeline.** Closed a gap left open since Phase A: nothing had ever actually run the real pipeline against the hand-calculated fixtures and diffed the output. Built a required GitHub Actions workflow with a real `postgres:16` service container, real `dbt run`/`dbt test`, and a DAG-structure check, zero dependency on the survey CDN, so a PR is never blocked by an external service. Found and fixed a real deployment bug: the first CI run crashed on teardown due to a protobuf 5 incompatibility with dbt 1.7.0.

**Phase D, failure and reproducibility.** The most demanding phase: six real failures triggered against a live, fully-running Docker Compose stack, not simulated, not mocked. A truncated source file, a process killed mid raw-table reload, Postgres killed mid dbt run, a real downstream test failure through Airflow, a process killed right after publish commits, two overlapping real DAG runs. Found and fixed two real bugs: a truncated CSV could silently publish (fixed with a new row-count-drop data quality check), and the production ingest URL had gone dead (a live 404, fixed and re-verified with a real ingest). Also measured real baseline runtimes against the actual roughly 65,000-row dataset, five repeated runs, machine specs recorded, ranges reported rather than a single cherry-picked number.

**Phase E, a second real survey year.** Threaded `survey_year` as a first-class column through the entire schema: raw table, staging grain, release tracking, and the active-release pointer, which moved from a single warehouse-wide pointer to one pointer per year. This is what let 2023 and 2024 coexist as independently published, simultaneously queryable data. Caught the cross-year monotonicity risk described in section 3.3 before it shipped. Made an explicit, tested decision for the two genuinely missing 2023 columns: report NULL, never a fabricated `0.0`, because a zero would misrepresent "the question wasn't asked" as "nobody sees AI as a threat." Ingested the real 2023 CDN data end to end and confirmed both years correctly coexist.

**Phase F, a small, trustworthy dashboard, and a bug it caught.** Built a static, honestly-labeled dashboard ("data as of `<published_at>`," not a live query) rendered server-side with inline SVG and zero client-side computation, so every number on the page is literally readable out of the generated HTML. The "not asked" badge logic is driven generically by a real NULL in the query result, not hardcoded to a specific year. A rendering failure is proven, with a regression test, to be unable to touch a release that already published. While re-rendering the dashboard against real data rather than fixtures, the project caught a genuinely significant, previously invisible bug (section 6).

## 6. The Most Important Finding: `avg_job_satisfaction` Was Silently Broken Since Phase A

The `mart_ai_sentiment` model filtered `job_sat` values through the regex `^[0-9]+$` before averaging. Real 2024 `job_sat` values are formatted with a decimal point (`'8.0'`, `'7.0'`, and so on), which that regex rejects. The result: every one of the 3,828 published AI-sentiment cells for 2024 had a NULL job satisfaction average, not because the question wasn't asked, but because the regex could not parse the real answer format. 29,126 raw 2024 rows had an actual `job_sat` value; zero of them ever survived into a published average, in any phase, since Phase A.

This had been true the entire time and nothing caught it, because the hand-calculated test fixtures never happened to use a decimal-formatted value. It surfaced only because Phase F's dashboard forced an honest look at real per-cell coverage: every 2024 cell was about to display a "Not asked in 2024" badge, which would have been false. The fix widened the regex to `^[0-9]+(\.[0-9]+)?$`; after a real republish, 3,374 of 3,828 cells show a genuine average (range 0.00 to 10.00, mean 6.89 across cells), with the remaining 454 correctly still NULL (no numeric answer in that cell). A regression test using a decimal-formatted fixture value was added so this exact bug class can't silently ship again, and the finding is documented plainly in `docs/data_contracts.md` rather than quietly patched.

This is the clearest evidence in the whole project that the process, build something real, then look hard enough at real data to find what's actually wrong with it, works as intended.

## 7. Data Quality Checks

Checks run on `raw.survey_responses`, scoped per survey year, and log to `dwh.dq_issues`:

1. **null_response_id**: rows with a null primary key
2. **duplicate_response_id**: duplicate `response_id` values within the same year (the same id appearing in both 2023 and 2024 is not a duplicate)
3. **null_country**, **null_comp_total**: monitoring-only, log but don't block
4. **invalid_years_code_pro**: non-numeric, non-sentinel values in an experience field
5. **row_count_drop**: blocks publication if a new load's row count falls more than 50% below the last published release for that year, added in Phase D after a truncated file was found to silently publish
6. **total_rows_loaded**: audit total, blocks only at exactly zero

## 8. Known, Stated Limitations

Documented in `docs/known_limitations.md`, not hidden: the raw ingest layer is year-scoped delete-and-reload, not append-only, so a kill between the delete and the insert can leave that year's raw table briefly empty (mart-level publication safety is unaffected: a bad raw load either blocks on DQ or never gets promoted, but the raw table itself has this window). CI exercises the same Python modules and dbt CLI the Airflow DAG uses, but does not run the actual Airflow scheduler; task order is checked structurally, not by a live end-to-end DAG execution. Both are deliberate scope boundaries, stated as such.

## 9. Verification Discipline

Every phase in this project was verified independently before being considered complete, not accepted on the strength of a report alone. That meant, repeatedly: cloning the repo fresh rather than trusting a local diff, reading the actual changed code rather than the summary of it, spinning up a real Postgres container and re-running the full test suite rather than trusting a cached "tests passed," and once CI existed, fetching the live GitHub Actions run directly to confirm it was actually green on the actual PR rather than trusting the merged page's own summary. Several real bugs and process gaps in this report were caught precisely because of that discipline, including the job-satisfaction regex bug in section 6, which a less thorough process would have shipped invisibly.

## 10. Deliverables

- Warehouse schema and release-safety tables (`scripts/init_db.sql`, `scripts/migrate_release_safety.sql`)
- Ingestion and data quality scripts (`scripts/ingest_survey.py`, `scripts/dq_checks.py`)
- Publication logic (`scripts/release.py`), open/candidate/publish, per-year monotonicity guard
- dbt project, staging, intermediate, and mart models, with schema tests (`dbt_project/`)
- Airflow DAG, full weekly pipeline including dashboard rendering (`airflow/dags/stackoverflow_pipeline_dag.py`)
- Static dashboard generator (`scripts/render_dashboard.py`, `docs/site/`)
- Required and scheduled CI (`.github/workflows/ci.yml`, `.github/workflows/real-dataset-check.yml`)
- Reusable failure-injection scripts (`scripts/chaos/`)
- Documentation of real findings: `docs/data_contracts.md`, `docs/data_quality_policy.md`, `docs/failure_modes.md`, `docs/reproducibility.md`, `docs/known_limitations.md`

Together, these provide a pipeline from two real survey years to salary, technology-adoption, and AI-sentiment analytics that has been tested against real failures, verified against a real changing data source, and caught and fixed a real correctness bug that would otherwise still be silently wrong.
