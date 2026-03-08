
  create view "survey_db"."dwh_staging"."stg_survey_responses__dbt_tmp"
    
    
  as (
    WITH deduped AS (
    SELECT DISTINCT ON (response_id) *
    FROM "survey_db"."raw"."survey_responses"
    ORDER BY response_id, loaded_at DESC NULLS LAST
)
SELECT
    response_id,
    main_branch,
    age,
    remote_work,
    ed_level,
    CASE
        WHEN years_code = 'Less than 1 year' THEN 0
        WHEN years_code = 'More than 50 years' THEN 51
        ELSE CAST(years_code AS NUMERIC)
    END AS years_code,
    CASE
        WHEN years_code_pro = 'Less than 1 year' THEN 0
        WHEN years_code_pro = 'More than 50 years' THEN 51
        ELSE CAST(years_code_pro AS NUMERIC)
    END AS years_code_pro,
    country,
    currency,
    dev_type,
    org_size,
    industry,
    language_have_worked,
    language_want_work,
    database_have_worked,
    database_want_work,
    platform_have_worked,
    platform_want_work,
    ai_select,
    ai_sent,
    ai_threat,
    job_sat,
    CAST(NULLIF(REGEXP_REPLACE(comp_total, '[^0-9.]', '', 'g'), '') AS NUMERIC) AS comp_total_usd,
    loaded_at
FROM deduped
WHERE response_id IS NOT NULL
  );