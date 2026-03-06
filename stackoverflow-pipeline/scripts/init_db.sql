-- Create survey database and set context
CREATE DATABASE survey_db;
\connect survey_db;

-- Schemas
CREATE SCHEMA raw;
CREATE SCHEMA dwh;

-- Data quality issues table
CREATE TABLE dwh.dq_issues (
    id SERIAL PRIMARY KEY,
    check_name TEXT,
    issue_type TEXT,
    row_count INTEGER,
    details TEXT,
    logged_at TIMESTAMP DEFAULT NOW()
);

-- Raw survey responses table
CREATE TABLE raw.survey_responses (
    response_id TEXT,
    main_branch TEXT,
    age TEXT,
    remote_work TEXT,
    ed_level TEXT,
    years_code TEXT,
    years_code_pro TEXT,
    dev_type TEXT,
    org_size TEXT,
    country TEXT,
    currency TEXT,
    comp_total TEXT,
    language_have_worked TEXT,
    language_want_work TEXT,
    database_have_worked TEXT,
    database_want_work TEXT,
    platform_have_worked TEXT,
    platform_want_work TEXT,
    ai_select TEXT,
    ai_sent TEXT,
    ai_threat TEXT,
    job_sat TEXT,
    industry TEXT,
    loaded_at TIMESTAMP DEFAULT NOW()
);
