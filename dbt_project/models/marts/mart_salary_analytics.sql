-- Salary cell: country × experience band × role × remote × org size,
-- plus release_id and survey_year so many runs and years can coexist.
--
-- Grain of one run: those five attributes for one survey_year. Physical
-- grain is (release_id, survey_year, country, experience_band, dev_type,
-- remote_work, org_size). Readers use marts.v_salary_analytics, which
-- joins dwh.active_release on (survey_year, release_id).
--
-- Append-only: dbt incremental INSERT. Never DROP this table on a run;
-- a half-finished run would otherwise become the official answer.
--
-- FROM is stg_survey_responses only. We do **not** join
-- int_languages_exploded or int_databases_exploded. Those models fan out to
-- one row per tech token; joining them here would repeat the same salary
-- (see tests/fixtures/fanout_counterfactual.md and docs/data_contracts.md).
--
-- Raw aggregates (avg_salary, median_salary, p25/p75/min/max_salary) are of
-- comp_total_raw (self-reported CompTotal in the respondent's own
-- currency) — unchanged from before, still mixed currencies, still here so
-- a reader who wants the untouched self-reported number always has it.
--
-- USD aggregates (avg_salary_usd, median_salary_usd, p25/p75/min/max_salary_usd)
-- are of comp_total_usd_converted, added in the currency-normalization work
-- (see docs/data_contracts.md and docs/engineering_journal.md entry 8).
-- comp_total_usd_converted is NULL for a respondent whose currency_code has
-- no row in seeds/fx_rates_to_usd.csv (not one of the ~31 ECB-published
-- currencies); Postgres AVG/PERCENTILE_CONT/MIN/MAX already ignore NULLs,
-- so a cell with partial currency coverage still gets a USD number from the
-- rows that did convert, not a NULL or a silently wrong one.
-- fx_converted_count says, per cell, how many of respondent_count actually
-- had a usable FX rate, so a reader can see that coverage instead of
-- assuming the USD columns cover every respondent in the cell.
--
-- This run only writes the year it was opened for. Other years stay in
-- staging but must not land under this release_id.

{% if var('release_id', none) is none %}
  {{ exceptions.raise_compiler_error('release_id var is required; the DAG must pass --vars') }}
{% endif %}
{% if var('survey_year', none) is none %}
  {{ exceptions.raise_compiler_error('survey_year var is required; the DAG must pass --vars') }}
{% endif %}

{{ config(
    materialized='incremental',
    incremental_strategy='append',
    full_refresh=false,
    pre_hook="{% if is_incremental() %}DELETE FROM {{ this }} WHERE release_id = '{{ var('release_id') }}'::uuid{% endif %}"
) }}

SELECT
    '{{ var("release_id") }}'::uuid AS release_id,
    survey_year,
    country,
    CASE
        WHEN years_code_pro IS NULL THEN 'Unknown'
        WHEN years_code_pro < 2 THEN '0-1 years'
        WHEN years_code_pro < 5 THEN '2-4 years'
        WHEN years_code_pro < 10 THEN '5-9 years'
        WHEN years_code_pro < 20 THEN '10-19 years'
        ELSE '20+ years'
    END AS experience_band,
    dev_type,
    remote_work,
    org_size,
    COUNT(*) AS respondent_count,
    ROUND(AVG(comp_total_raw)::NUMERIC, 2) AS avg_salary,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY comp_total_raw))::NUMERIC, 2) AS median_salary,
    ROUND((PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY comp_total_raw))::NUMERIC, 2) AS p25_salary,
    ROUND((PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY comp_total_raw))::NUMERIC, 2) AS p75_salary,
    ROUND(MIN(comp_total_raw)::NUMERIC, 2) AS min_salary,
    ROUND(MAX(comp_total_raw)::NUMERIC, 2) AS max_salary,
    COUNT(comp_total_usd_converted) AS fx_converted_count,
    ROUND(AVG(comp_total_usd_converted)::NUMERIC, 2) AS avg_salary_usd,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY comp_total_usd_converted))::NUMERIC, 2) AS median_salary_usd,
    ROUND((PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY comp_total_usd_converted))::NUMERIC, 2) AS p25_salary_usd,
    ROUND((PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY comp_total_usd_converted))::NUMERIC, 2) AS p75_salary_usd,
    ROUND(MIN(comp_total_usd_converted)::NUMERIC, 2) AS min_salary_usd,
    ROUND(MAX(comp_total_usd_converted)::NUMERIC, 2) AS max_salary_usd
FROM {{ ref('stg_survey_responses') }}
WHERE survey_year = {{ var('survey_year') | int }}
  AND comp_total_raw IS NOT NULL
  AND comp_total_raw >= 10000
  AND comp_total_raw <= 5000000
  AND country IS NOT NULL
GROUP BY
    survey_year,
    country,
    CASE
        WHEN years_code_pro IS NULL THEN 'Unknown'
        WHEN years_code_pro < 2 THEN '0-1 years'
        WHEN years_code_pro < 5 THEN '2-4 years'
        WHEN years_code_pro < 10 THEN '5-9 years'
        WHEN years_code_pro < 20 THEN '10-19 years'
        ELSE '20+ years'
    END,
    dev_type,
    remote_work,
    org_size
HAVING COUNT(*) >= 5
