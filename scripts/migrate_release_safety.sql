-- Publication-safety tables, append-only mart shells, and reader views.
--
-- Idempotent: safe on a fresh warehouse and on a warehouse that already ran
-- Phase A (overwrite marts). Old mart tables without release_id are dropped;
-- they only ever held one generation of data anyway.
--
-- Readers must use marts.v_* not marts.mart_*. The tables keep history;
-- the views show the one row-set in dwh.active_release.
--
-- Docker entrypoint runs each file in POSTGRES_DB (airflow). \connect jumps
-- to survey_db. pytest strips this line and runs against survey_db directly.

\connect survey_db

CREATE SCHEMA IF NOT EXISTS dwh;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS marts;

CREATE TABLE IF NOT EXISTS dwh.dq_issues (
    id SERIAL PRIMARY KEY,
    check_name TEXT,
    issue_type TEXT,
    row_count INTEGER,
    details TEXT,
    logged_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS raw.survey_responses (
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

CREATE TABLE IF NOT EXISTS dwh.pipeline_releases (
    release_id UUID PRIMARY KEY,
    seq BIGSERIAL NOT NULL UNIQUE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_checksum TEXT,
    git_sha TEXT,
    status TEXT NOT NULL,
    dq_summary JSONB,
    published_at TIMESTAMPTZ,
    unchanged_source BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    CONSTRAINT pipeline_releases_status_ok CHECK (status IN (
        'building',
        'candidate_ready',
        'candidate_ready_unchanged_source',
        'published',
        'failed',
        'superseded'
    ))
);

-- Existing warehouses created before seq/superseded: add the column and
-- widen the status check. Fresh CREATE TABLE above already has both.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'dwh'
          AND table_name = 'pipeline_releases'
          AND column_name = 'seq'
    ) THEN
        CREATE SEQUENCE IF NOT EXISTS dwh.pipeline_releases_seq;
        ALTER TABLE dwh.pipeline_releases
            ADD COLUMN seq BIGINT;
        ALTER SEQUENCE dwh.pipeline_releases_seq OWNED BY dwh.pipeline_releases.seq;
        UPDATE dwh.pipeline_releases
            SET seq = nextval('dwh.pipeline_releases_seq')
            WHERE seq IS NULL;
        ALTER TABLE dwh.pipeline_releases
            ALTER COLUMN seq SET DEFAULT nextval('dwh.pipeline_releases_seq');
        ALTER TABLE dwh.pipeline_releases
            ALTER COLUMN seq SET NOT NULL;
        ALTER TABLE dwh.pipeline_releases
            ADD CONSTRAINT pipeline_releases_seq_unique UNIQUE (seq);
    END IF;

    ALTER TABLE dwh.pipeline_releases
        DROP CONSTRAINT IF EXISTS pipeline_releases_status_ok;
    ALTER TABLE dwh.pipeline_releases
        ADD CONSTRAINT pipeline_releases_status_ok CHECK (status IN (
            'building',
            'candidate_ready',
            'candidate_ready_unchanged_source',
            'published',
            'failed',
            'superseded'
        ));
END $$;


-- Exactly one live pointer. CHECK (singleton) + PRIMARY KEY means at most
-- one row. Empty table = nothing published yet (views return zero rows).
CREATE TABLE IF NOT EXISTS dwh.active_release (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Drop overwrite-era marts that have no release_id so dbt/tests can recreate.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'marts' AND table_name = 'mart_salary_analytics'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts'
          AND table_name = 'mart_salary_analytics'
          AND column_name = 'release_id'
    ) THEN
        DROP VIEW IF EXISTS marts.v_salary_analytics;
        DROP TABLE marts.mart_salary_analytics;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'marts' AND table_name = 'mart_tech_adoption'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts'
          AND table_name = 'mart_tech_adoption'
          AND column_name = 'release_id'
    ) THEN
        DROP VIEW IF EXISTS marts.v_tech_adoption;
        DROP TABLE marts.mart_tech_adoption;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'marts' AND table_name = 'mart_ai_sentiment'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts'
          AND table_name = 'mart_ai_sentiment'
          AND column_name = 'release_id'
    ) THEN
        DROP VIEW IF EXISTS marts.v_ai_sentiment;
        DROP TABLE marts.mart_ai_sentiment;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS marts.mart_salary_analytics (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    country TEXT,
    experience_band TEXT,
    dev_type TEXT,
    remote_work TEXT,
    org_size TEXT,
    respondent_count BIGINT,
    avg_salary NUMERIC,
    median_salary NUMERIC,
    p25_salary NUMERIC,
    p75_salary NUMERIC,
    min_salary NUMERIC,
    max_salary NUMERIC
);

CREATE TABLE IF NOT EXISTS marts.mart_tech_adoption (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    tech_name TEXT,
    total_users BIGINT,
    want_to_continue_count BIGINT,
    retention_rate_pct NUMERIC,
    tech_type TEXT,
    usage_rank BIGINT
);

CREATE TABLE IF NOT EXISTS marts.mart_ai_sentiment (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    country TEXT,
    dev_type TEXT,
    ai_select TEXT,
    ai_sent TEXT,
    ai_threat TEXT,
    respondent_count BIGINT,
    avg_job_satisfaction NUMERIC,
    pct_see_ai_as_threat NUMERIC
);

CREATE INDEX IF NOT EXISTS mart_salary_analytics_release_idx
    ON marts.mart_salary_analytics (release_id);
CREATE INDEX IF NOT EXISTS mart_tech_adoption_release_idx
    ON marts.mart_tech_adoption (release_id);
CREATE INDEX IF NOT EXISTS mart_ai_sentiment_release_idx
    ON marts.mart_ai_sentiment (release_id);

-- Reader contract: never scan the physical marts without this filter.
CREATE OR REPLACE VIEW marts.v_salary_analytics AS
SELECT *
FROM marts.mart_salary_analytics
WHERE release_id = (SELECT release_id FROM dwh.active_release);

CREATE OR REPLACE VIEW marts.v_tech_adoption AS
SELECT *
FROM marts.mart_tech_adoption
WHERE release_id = (SELECT release_id FROM dwh.active_release);

CREATE OR REPLACE VIEW marts.v_ai_sentiment AS
SELECT *
FROM marts.mart_ai_sentiment
WHERE release_id = (SELECT release_id FROM dwh.active_release);
