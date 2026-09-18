-- Staging: one clean row per (survey_year, respondent).
--
-- Grain: (survey_year, response_id). The same ResponseId can appear in 2023
-- and 2024; those are different people-years, not duplicates. Within one
-- year, raw can contain duplicates (see fixture R008); we keep the latest
-- loaded_at so a re-ingest or a repeated ResponseId does not double-count
-- that person downstream.
--
-- Experience sentinels become numbers here because the salary mart bands on
-- numeric years_code_pro. "Less than 1 year" → 0 (band 0-1). "More than 50
-- years" → 51 (band 20+).
--
-- comp_total_raw is the survey CompTotal stripped to a number. currency is
-- stored but not applied. USD conversion is not implemented (Stack Overflow
-- uses the 11 Jun 2024 FX rate; we do not). Null response_id is dropped
-- because nothing downstream can join on it.
--
-- survey_year is copied from raw, not inferred. 2023 rows have NULL
-- ai_threat and job_sat because those columns do not exist in the 2023
-- extract (see docs/data_contracts.md).

WITH deduped AS (
    SELECT DISTINCT ON (survey_year, response_id) *
    FROM {{ source('raw', 'survey_responses') }}
    ORDER BY survey_year, response_id, loaded_at DESC NULLS LAST
)
SELECT
    survey_year,
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
    CAST(NULLIF(REGEXP_REPLACE(comp_total, '[^0-9.]', '', 'g'), '') AS NUMERIC) AS comp_total_raw,
    loaded_at
FROM deduped
WHERE response_id IS NOT NULL
