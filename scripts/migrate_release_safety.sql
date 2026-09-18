-- Publication-safety tables, append-only mart shells, and reader views.
--
-- Idempotent: safe on a fresh warehouse and on a warehouse that already ran
-- Phase A (overwrite marts) or Phase B (singleton active_release).
--
-- Phase E: survey_year is a real dimension. raw, pipeline_releases, marts,
-- and dwh.active_release all carry it. active_release is one row per year
-- (survey_year PK), not a singleton. Readers use marts.v_* which join
-- active_release on (survey_year, release_id) so both years can be live.
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
    loaded_at TIMESTAMP DEFAULT NOW(),
    survey_year INTEGER
);

-- Existing warehouses (Phase A–D) have no survey_year. The only year ever
-- ingested before Phase E is 2024; stamp those rows rather than DROP.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'raw'
          AND table_name = 'survey_responses'
          AND column_name = 'survey_year'
    ) THEN
        ALTER TABLE raw.survey_responses ADD COLUMN survey_year INTEGER;
    END IF;
    UPDATE raw.survey_responses SET survey_year = 2024 WHERE survey_year IS NULL;
    ALTER TABLE raw.survey_responses ALTER COLUMN survey_year SET NOT NULL;
END $$;

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
    survey_year INTEGER,
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

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'dwh'
          AND table_name = 'pipeline_releases'
          AND column_name = 'survey_year'
    ) THEN
        ALTER TABLE dwh.pipeline_releases ADD COLUMN survey_year INTEGER;
    END IF;
    UPDATE dwh.pipeline_releases SET survey_year = 2024 WHERE survey_year IS NULL;
    ALTER TABLE dwh.pipeline_releases ALTER COLUMN survey_year SET NOT NULL;
END $$;


-- Views currently depend on dwh.active_release; drop them before rebuilding
-- the pointer table from singleton BOOLEAN PK → survey_year INTEGER PK.
DROP VIEW IF EXISTS marts.v_salary_analytics;
DROP VIEW IF EXISTS marts.v_tech_adoption;
DROP VIEW IF EXISTS marts.v_ai_sentiment;

-- Phase B singleton → Phase E one active pointer per survey_year.
-- Preserve the currently published production release as 2024 (the only
-- year ingested before this migration). Do not DROP and recreate empty.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'dwh'
          AND table_name = 'active_release'
          AND column_name = 'singleton'
    ) THEN
        CREATE TABLE dwh.active_release_by_year (
            survey_year INTEGER PRIMARY KEY,
            release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        INSERT INTO dwh.active_release_by_year (survey_year, release_id, updated_at)
        SELECT COALESCE(r.survey_year, 2024), a.release_id, a.updated_at
        FROM dwh.active_release a
        LEFT JOIN dwh.pipeline_releases r ON r.release_id = a.release_id
        ON CONFLICT (survey_year) DO NOTHING;
        DROP TABLE dwh.active_release;
        ALTER TABLE dwh.active_release_by_year RENAME TO active_release;
    ELSIF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'dwh' AND table_name = 'active_release'
    ) THEN
        CREATE TABLE dwh.active_release (
            survey_year INTEGER PRIMARY KEY,
            release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    END IF;
END $$;

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
        DROP TABLE marts.mart_ai_sentiment;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS marts.mart_salary_analytics (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    survey_year INTEGER,
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
    max_salary NUMERIC,
    fx_converted_count BIGINT,
    avg_salary_usd NUMERIC,
    median_salary_usd NUMERIC,
    p25_salary_usd NUMERIC,
    p75_salary_usd NUMERIC,
    min_salary_usd NUMERIC,
    max_salary_usd NUMERIC
);

CREATE TABLE IF NOT EXISTS marts.mart_tech_adoption (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    survey_year INTEGER,
    tech_name TEXT,
    total_users BIGINT,
    want_to_continue_count BIGINT,
    retention_rate_pct NUMERIC,
    tech_type TEXT,
    usage_rank BIGINT
);

CREATE TABLE IF NOT EXISTS marts.mart_ai_sentiment (
    release_id UUID NOT NULL REFERENCES dwh.pipeline_releases (release_id),
    survey_year INTEGER,
    country TEXT,
    dev_type TEXT,
    ai_select TEXT,
    ai_sent TEXT,
    ai_threat TEXT,
    respondent_count BIGINT,
    avg_job_satisfaction NUMERIC,
    pct_see_ai_as_threat NUMERIC
);

-- Existing Phase B marts: add survey_year and stamp 2024. Fresh CREATE above
-- already has the column (nullable until this block).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'mart_salary_analytics'
          AND column_name = 'survey_year'
    ) THEN
        ALTER TABLE marts.mart_salary_analytics ADD COLUMN survey_year INTEGER;
    END IF;
    UPDATE marts.mart_salary_analytics SET survey_year = 2024 WHERE survey_year IS NULL;
    ALTER TABLE marts.mart_salary_analytics ALTER COLUMN survey_year SET NOT NULL;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'mart_tech_adoption'
          AND column_name = 'survey_year'
    ) THEN
        ALTER TABLE marts.mart_tech_adoption ADD COLUMN survey_year INTEGER;
    END IF;
    UPDATE marts.mart_tech_adoption SET survey_year = 2024 WHERE survey_year IS NULL;
    ALTER TABLE marts.mart_tech_adoption ALTER COLUMN survey_year SET NOT NULL;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'marts' AND table_name = 'mart_ai_sentiment'
          AND column_name = 'survey_year'
    ) THEN
        ALTER TABLE marts.mart_ai_sentiment ADD COLUMN survey_year INTEGER;
    END IF;
    UPDATE marts.mart_ai_sentiment SET survey_year = 2024 WHERE survey_year IS NULL;
    ALTER TABLE marts.mart_ai_sentiment ALTER COLUMN survey_year SET NOT NULL;
END $$;

-- Currency normalization: add USD aggregate columns to an already-existing
-- mart_salary_analytics (a fresh CREATE TABLE IF NOT EXISTS above already
-- has them; this covers a database migrated before this change).
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS fx_converted_count BIGINT;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS avg_salary_usd NUMERIC;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS median_salary_usd NUMERIC;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS p25_salary_usd NUMERIC;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS p75_salary_usd NUMERIC;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS min_salary_usd NUMERIC;
ALTER TABLE marts.mart_salary_analytics ADD COLUMN IF NOT EXISTS max_salary_usd NUMERIC;

CREATE INDEX IF NOT EXISTS mart_salary_analytics_release_idx
    ON marts.mart_salary_analytics (release_id);
CREATE INDEX IF NOT EXISTS mart_tech_adoption_release_idx
    ON marts.mart_tech_adoption (release_id);
CREATE INDEX IF NOT EXISTS mart_ai_sentiment_release_idx
    ON marts.mart_ai_sentiment (release_id);
CREATE INDEX IF NOT EXISTS mart_salary_analytics_year_idx
    ON marts.mart_salary_analytics (survey_year, release_id);
CREATE INDEX IF NOT EXISTS mart_tech_adoption_year_idx
    ON marts.mart_tech_adoption (survey_year, release_id);
CREATE INDEX IF NOT EXISTS mart_ai_sentiment_year_idx
    ON marts.mart_ai_sentiment (survey_year, release_id);

-- Reader contract: each year independently reflects that year's active
-- release. Two published years are both visible. Empty active_release
-- means the view is empty.
CREATE OR REPLACE VIEW marts.v_salary_analytics AS
SELECT m.*
FROM marts.mart_salary_analytics m
JOIN dwh.active_release ar
  ON ar.survey_year = m.survey_year
 AND ar.release_id = m.release_id;

CREATE OR REPLACE VIEW marts.v_tech_adoption AS
SELECT m.*
FROM marts.mart_tech_adoption m
JOIN dwh.active_release ar
  ON ar.survey_year = m.survey_year
 AND ar.release_id = m.release_id;

CREATE OR REPLACE VIEW marts.v_ai_sentiment AS
SELECT m.*
FROM marts.mart_ai_sentiment m
JOIN dwh.active_release ar
  ON ar.survey_year = m.survey_year
 AND ar.release_id = m.release_id;
