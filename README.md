# Stack Overflow Developer Survey 2024 — Analytics Pipeline

An end-to-end data engineering pipeline that ingests the Stack Overflow Developer Survey 2024 (65,000+ responses, 114 columns), applies structured data quality checks, transforms the data through a layered dbt model architecture, and exposes three analytical marts covering **salary benchmarks**, **technology adoption**, and **AI sentiment trends**.

---

## Architecture

```
┌─────────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Survey ZIP (CDN)    │────▶│  ingest_survey   │────▶│  raw.survey_    │────▶│  run_dq_checks  │
│  survey_results_     │     │  (Python)        │     │  responses      │     │  → dwh.dq_issues │
└─────────────────────┘     └──────────────────┘     └────────┬────────┘     └────────┬─────────┘
                                                               │                      │
                                                               ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│  dbt run:  staging (stg_survey_responses) → intermediate (int_*_exploded) → marts            │
│  marts: mart_salary_analytics | mart_tech_adoption | mart_ai_sentiment                         │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                               │
                                                               ▼
┌─────────────────────┐
│  dbt test           │  (uniqueness, not_null, accepted_values on models)
└─────────────────────┘
```

**Stack:** Apache Airflow 2.8 (orchestration) · PostgreSQL 16 (warehouse) · dbt (transformations) · Python (ingest + DQ) · Docker Compose (runtime)

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- On Linux/Mac, optional: set `AIRFLOW_UID` (e.g. `export AIRFLOW_UID=$(id -u)`) so Airflow can write logs

---

## Quick Start

1. **Start Postgres and Airflow (first time: init DB + create admin user)**

   ```bash
   cd stackoverflow-pipeline
   docker compose up -d postgres
   docker compose run --rm airflow-init
   docker compose up -d airflow-webserver airflow-scheduler
   ```

2. **Open Airflow UI**

   - URL: http://localhost:8080  
   - Login: `admin` / `admin`

3. **Run the pipeline**

   - Unpause the DAG **`stackoverflow_survey_pipeline`** (toggle on the left).
   - Trigger a run manually or wait for the weekly schedule.
   - Order of tasks: `ingest_raw_survey` → `run_dq_checks` → `dbt_run_models` → `dbt_test_models`.

4. **Query the results**

   - Connect to Postgres (host `localhost`, port `5432`, user `airflow`, password `airflow`).  
   - Database `survey_db` contains schemas `raw`, `staging`, `intermediate`, `marts`, and `dwh`.

---

## Project Structure

```
stackoverflow-pipeline/
├── airflow/
│   └── dags/
│       └── stackoverflow_pipeline_dag.py   # Weekly DAG: ingest → DQ → dbt run → dbt test
├── scripts/
│   ├── init_db.sql                        # Creates survey_db, raw + dwh schemas, tables
│   ├── ingest_survey.py                   # Download ZIP, extract CSV, clean, load raw.survey_responses
│   └── dq_checks.py                       # Six checks on raw data → dwh.dq_issues
├── dbt_project/
│   ├── dbt_project.yml
│   ├── models/
│   │   ├── staging/                       # stg_survey_responses (dedupe, cast, clean)
│   │   ├── intermediate/                  # int_languages_exploded, int_databases_exploded
│   │   └── marts/                         # mart_salary_analytics, mart_tech_adoption, mart_ai_sentiment
│   └── ...
├── docker-compose.yml
├── README.md
└── PROJECT_SUMMARY_REPORT.md              # Detailed report: goals, approach, dataset, checks
```

---

## Pipeline Tasks

| Task | Description |
| --- | --- |
| **open_release** | Inserts `dwh.pipeline_releases` (`building`, with `survey_year`) and pushes `release_id` to XCom. |
| **ingest_raw_survey** | Downloads that year's survey CSV, cleans it, replaces `raw.survey_responses` **for that year only**. |
| **record_source_checksum** | Hashes that year's raw rows; flags identical-to-published source for the same year (still builds). |
| **run_dq_checks** | Checks scoped to `survey_year` → `dwh.dq_issues`. Blocking checks fail the DAG (see `docs/data_quality_policy.md`). |
| **dbt_run_models** | Staging → intermediate → append mart rows tagged with `release_id` and `survey_year`. |
| **dbt_test_models** | dbt tests, scoped to this `release_id` for mart not_null checks. |
| **mark_candidate** | `candidate_ready` or `candidate_ready_unchanged_source`. Does **not** move the live pointer. |
| **publish_release** | One locked transaction: this year's `dwh.active_release` row := this candidate. |

Readers query `marts.v_salary_analytics`, `marts.v_tech_adoption`, `marts.v_ai_sentiment` (join to the per-year active release). Do not query `marts.mart_*` for official numbers.

Trigger a 2023 run with DAG conf `{"survey_year": 2023}` or env `SURVEY_YEAR=2023`. Default is 2024.

---

## Data Model (dbt)

- Staging: `stg_survey_responses` — One row per `(survey_year, response_id)`; numeric casting for `years_code`, `years_code_pro`, `comp_total_raw` (local currency as entered; USD conversion is not implemented).
- **Intermediate:** `int_languages_exploded`, `int_databases_exploded` — One row per (response, language) or (response, database); includes `wants_to_continue` from “want to work with” fields.
- **Marts:**
  - **mart_salary_analytics** — Append-only per `release_id`. Official read is `marts.v_salary_analytics`. Aggregations by country, experience band, dev type, remote work, org size; filters e.g. 10k–5M, ≥5 respondents.
  - **mart_tech_adoption** — Languages and databases: total users, want-to-continue count, retention %; usage rank by tech type; filters e.g. ≥100 users.
  - **mart_ai_sentiment** — By country and dev type: AI tool usage/sentiment; respondent count, optional job satisfaction and % seeing AI as threat; minimum 3 respondents.

---

## Data Source

- **Survey:** [Stack Overflow Developer Survey 2024](https://survey.stackoverflow.co/2024/)  
- **Dataset:** Public ZIP at `https://survey.stackoverflow.co/datasets/stack-overflow-developer-survey-2024.zip` (CSV: 65,000+ rows, 114 columns).  
- The pipeline uses a subset of columns (identity, demographics, experience, work, compensation, languages, databases, platforms, AI-related fields); see `scripts/ingest_survey.py` and [PROJECT_SUMMARY_REPORT.md](PROJECT_SUMMARY_REPORT.md) for details.

---

## Environment Variables (optional)

For ingest and DQ scripts (defaults work with Docker Compose):

| Variable             | Default    | Description        |
|----------------------|------------|--------------------|
| `SURVEY_DB_HOST`     | `postgres` | PostgreSQL host    |
| `SURVEY_DB_PORT`     | `5432`     | PostgreSQL port   |
| `SURVEY_DB_NAME`     | `survey_db`| Database name      |
| `SURVEY_DB_USER`     | `airflow`  | Database user      |
| `SURVEY_DB_PASSWORD` | `airflow`  | Database password  |

---

## More Detail

For a full write-up (why we built it, approach, dataset description, data quality checks), see **[PROJECT_SUMMARY_REPORT.md](PROJECT_SUMMARY_REPORT.md)**.
