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
-- comp_total_raw is the survey CompTotal stripped to a number, in whatever
-- currency the respondent typed. currency_code and comp_total_usd_converted
-- are added here (see docs/data_contracts.md, "Currency normalization" and
-- docs/engineering_journal.md entry 8):
--
--   * currency_code is the leading 3-letter ISO code parsed out of the raw
--     `currency` free-text field. This handles a bare code ("USD") and a
--     code-plus-name string ("USD United States dollar") the same way, by
--     taking the first 3 letters and upper-casing them. A currency field
--     that does not start with 3 letters (blank, garbage) parses to NULL.
--   * comp_total_usd_converted is comp_total_raw multiplied by a single
--     fixed-date FX rate from seeds/fx_rates_to_usd.csv, keyed on
--     (survey_year, currency_code). The rate is a real historical European
--     Central Bank reference rate for one fixed date per survey year (the
--     same single-date-conversion approach Stack Overflow's own methodology
--     uses for 2024; see seeds/fx_rates_to_usd.csv header for exact dates
--     and sourcing). A currency_code with no row in that seed table (not
--     one of the ~31 currencies the ECB publishes daily) gets NULL here,
--     never a guessed or default rate.
--
-- Null response_id is dropped because nothing downstream can join on it.
--
-- survey_year is copied from raw, not inferred. 2023 rows have NULL
-- ai_threat and job_sat because those columns do not exist in the 2023
-- extract (see docs/data_contracts.md).

WITH deduped AS (
    SELECT DISTINCT ON (survey_year, response_id) *
    FROM {{ source('raw', 'survey_responses') }}
    ORDER BY survey_year, response_id, loaded_at DESC NULLS LAST
),
parsed AS (
    SELECT
        *,
        UPPER(SUBSTRING(TRIM(currency) FROM '^[A-Za-z]{3}')) AS currency_code
    FROM deduped
)
SELECT
    parsed.survey_year,
    parsed.response_id,
    parsed.main_branch,
    parsed.age,
    parsed.remote_work,
    parsed.ed_level,
    CASE
        WHEN parsed.years_code = 'Less than 1 year' THEN 0
        WHEN parsed.years_code = 'More than 50 years' THEN 51
        ELSE CAST(parsed.years_code AS NUMERIC)
    END AS years_code,
    CASE
        WHEN parsed.years_code_pro = 'Less than 1 year' THEN 0
        WHEN parsed.years_code_pro = 'More than 50 years' THEN 51
        ELSE CAST(parsed.years_code_pro AS NUMERIC)
    END AS years_code_pro,
    parsed.country,
    parsed.currency,
    parsed.currency_code,
    parsed.dev_type,
    parsed.org_size,
    parsed.industry,
    parsed.language_have_worked,
    parsed.language_want_work,
    parsed.database_have_worked,
    parsed.database_want_work,
    parsed.platform_have_worked,
    parsed.platform_want_work,
    parsed.ai_select,
    parsed.ai_sent,
    parsed.ai_threat,
    parsed.job_sat,
    CAST(NULLIF(REGEXP_REPLACE(parsed.comp_total, '[^0-9.]', '', 'g'), '') AS NUMERIC) AS comp_total_raw,
    ROUND(
        CAST(NULLIF(REGEXP_REPLACE(parsed.comp_total, '[^0-9.]', '', 'g'), '') AS NUMERIC)
        * CAST(fx.usd_per_unit AS NUMERIC),
        2
    ) AS comp_total_usd_converted,
    parsed.loaded_at
FROM parsed
LEFT JOIN {{ ref('fx_rates_to_usd') }} fx
  ON fx.survey_year = parsed.survey_year
 AND fx.currency_code = parsed.currency_code
WHERE parsed.response_id IS NOT NULL
