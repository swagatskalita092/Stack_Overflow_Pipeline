# Stack Overflow Developer Survey 2024 — Project Summary Report

## 1. What the Project Is About

This project is an **end-to-end data engineering pipeline** for the **Stack Overflow Developer Survey 2024**. It takes the official survey dataset (65,000+ responses, 114 columns), loads it into a PostgreSQL data warehouse, runs automated data quality checks, and transforms it through a layered dbt model into three analytical marts:

- **Salary analytics** — Compensation benchmarks by country, experience, role, remote work, and organization size  
- **Technology adoption** — Usage and “want to continue” retention for programming languages and databases  
- **AI sentiment** — How developers feel about AI tools, by country and role, with optional job-satisfaction and threat-perception metrics  

The pipeline is orchestrated by **Apache Airflow** on a weekly schedule and is designed to run in Docker (Postgres + Airflow) for a reproducible, production-style setup.

---

## 2. Why We Did It

- **Centralize and automate** — Replace ad hoc downloads and manual cleaning with a single, repeatable flow from source to analytics tables.  
- **Ensure data quality** — Catch issues (null keys, duplicates, invalid formats) early and log them in a structured way (`dwh.dq_issues`) before transformations run.  
- **Enable analytics** — Expose clean, aggregated marts so analysts and stakeholders can answer questions about salaries, tech adoption, and AI sentiment without touching raw data.  
- **Practice modern stack** — Use industry-standard tools: Airflow for orchestration, dbt for transformations and testing, Postgres as the warehouse, and Docker for environment consistency.

---

## 3. Approach

### 3.1 High-Level Flow

1. **Ingest** — Python script downloads the survey ZIP from the Stack Overflow CDN, extracts the CSV in memory, cleans it (column selection, renames, sentinel handling), and bulk-loads into `raw.survey_responses`.  
2. **Data quality** — A second Python script runs six predefined checks on the raw table (e.g. null `response_id`, duplicate `response_id`, null country/comp, invalid `years_code_pro`, plus an audit row count) and writes results to `dwh.dq_issues`.  
3. **Transform** — dbt runs a three-layer model: **staging** (dedupe, type casting, cleaning) → **intermediate** (exploded language/database arrays) → **marts** (aggregated salary, tech adoption, AI sentiment).  
4. **Test** — dbt tests run on the models (uniqueness, not-null, accepted values) to guard against regressions.

### 3.2 Technology Choices

| Component        | Choice                | Rationale                                                                 |
|-----------------|------------------------|----------------------------------------------------------------------------|
| Orchestration   | Apache Airflow 2.8     | DAG-based scheduling, retries, and clear task dependencies                |
| Warehouse       | PostgreSQL 16         | Single DB for raw, dwh, and dbt; simple to run in Docker                  |
| Transformations | dbt (dbt-postgres 1.7) | Layered models, schema tests, and SQL-first transformations               |
| Ingestion / DQ  | Python (pandas, psycopg2, requests) | Flexible cleaning and bulk insert; easy to extend checks          |
| Environment     | Docker Compose         | One-command bring-up for Postgres and Airflow with shared volumes         |

### 3.3 Data Model (Layers)

- **Raw** — `raw.survey_responses`: one row per survey response; columns aligned with a subset of the survey (e.g. `response_id`, demographics, comp, languages, databases, AI fields).  
- **Staging** — `stg_survey_responses`: deduplicated by `response_id`, numeric casting for `years_code` / `years_code_pro` and `comp_total` → `comp_total_usd`.  
- **Intermediate** — `int_languages_exploded`, `int_databases_exploded`: one row per response–language or response–database; “wants to continue” derived from the “want to work with” fields.  
- **Marts** — `mart_salary_analytics`, `mart_tech_adoption`, `mart_ai_sentiment`: aggregations with filters (e.g. salary band, minimum respondents) and dbt tests on key columns.

### 3.4 Orchestration (Airflow DAG)

- **DAG ID:** `stackoverflow_survey_pipeline`  
- **Schedule:** `@weekly`  
- **Tasks (order):** `ingest_raw_survey` → `run_dq_checks` → `dbt_run_models` → `dbt_test_models`  
- Scripts and dbt project are mounted into the Airflow image; the DAG calls the Python ingest and DQ modules and runs `dbt run` and `dbt test` in the dbt project directory.

---

## 4. The Dataset

### 4.1 Source

- **Name:** Stack Overflow Developer Survey 2024  
- **Provider:** Stack Overflow (survey.stackoverflow.co)  
- **Access:** Public ZIP at  
  `https://survey.stackoverflow.co/datasets/stack-overflow-developer-survey-2024.zip`  
- **Content:** Single CSV, `survey_results_public.csv`, inside the ZIP (65,000+ rows, 114 columns).

### 4.2 Scope Used in This Pipeline

The pipeline does not load all 114 columns. It selects and renames a subset for analytics and storage, including:

| Category        | Examples of columns used                                                                 |
|-----------------|-------------------------------------------------------------------------------------------|
| Identity        | `ResponseId` → `response_id`                                                              |
| Demographics    | `Age`, `Country`, `EdLevel`, `MainBranch`                                                 |
| Experience      | `YearsCode`, `YearsCodePro`                                                               |
| Work            | `RemoteWork`, `OrgSize`, `Industry`, `DevType`, `JobSat`                                 |
| Compensation     | `CompTotal`, `Currency`                                                                   |
| Tech (multi)    | `LanguageHaveWorkedWith` / `LanguageWantToWorkWith`, `DatabaseHaveWorkedWith` / `DatabaseWantToWorkWith`, `PlatformHaveWorkedWith` / `PlatformWantToWorkWith` |
| AI              | `AISelect`, `AISent`, `AIThreat`                                                         |

Multi-select fields are semicolon-separated in the CSV; the pipeline keeps them as text in raw/staging and explodes them in intermediate models (languages, databases) for the tech-adoption mart.

### 4.3 Cleaning and Conventions

- **Renames:** PascalCase source columns are renamed to snake_case (e.g. `CompTotal` → `comp_total`).  
- **Sentinels:** Values such as `"NA"`, `"N/A"`, `"nan"`, empty string are normalized to NULL before load.  
- **Numeric fields:** In dbt staging, `years_code` and `years_code_pro` special values (“Less than 1 year”, “More than 50 years”) are mapped to 0 and 51; `comp_total` is stripped of non-numeric characters and cast to numeric as `comp_total_usd`.  
- **Deduplication:** Staging uses `DISTINCT ON (response_id)` so each response appears once.

### 4.4 Data Quality Checks (Pre-dbt)

Six checks run on `raw.survey_responses` and log to `dwh.dq_issues`:

1. **null_response_id** — Count of rows with null `response_id` (primary key).  
2. **duplicate_response_id** — Count of duplicate `response_id` values.  
3. **null_country** — Rows with null country.  
4. **null_comp_total** — Rows with null compensation.  
5. **invalid_years_code_pro** — `years_code_pro` not null, not a special literal, and containing non-numeric characters.  
6. **total_rows_loaded** — Audit total row count.

These support monitoring and triage without blocking the pipeline; downstream models apply their own filters (e.g. non-null `response_id`, valid salary range).

---

## 5. Deliverables and Outcomes

- **Raw and DQ schema** — `raw.survey_responses`, `dwh.dq_issues`, created via `scripts/init_db.sql`.  
- **Ingestion script** — `scripts/ingest_survey.py`: download → extract → clean → load.  
- **DQ script** — `scripts/dq_checks.py`: run six checks and log to `dwh.dq_issues`.  
- **dbt project** — Staging, intermediate, and marts models plus schema tests in `dbt_project/models`.  
- **Airflow DAG** — `airflow/dags/stackoverflow_pipeline_dag.py`: weekly ingest → DQ → dbt run → dbt test.  
- **Docker Compose** — Postgres 16 and Airflow 2.8 with shared volumes for DAGs, scripts, and dbt project.

Together, these provide a documented, repeatable pipeline from the Stack Overflow Developer Survey 2024 dataset to salary, technology adoption, and AI sentiment analytics.
