-- Salary cell: country × experience band × role × remote × org size.
--
-- Grain is that five-way group, not a person and not a language. COUNT(*),
-- AVG, and PERCENTILE_CONT therefore count people.
--
-- FROM is stg_survey_responses only. We do **not** join
-- int_languages_exploded or int_databases_exploded. Those models fan out to
-- one row per tech token; joining them here would repeat the same salary
-- (see tests/fixtures/fanout_counterfactual.md and docs/data_contracts.md).
--
-- Filters: non-null country, compensation between 10k and 5M inclusive.
-- Those bounds are undocumented heuristics (not Stack Overflow's) and are
-- applied to mixed local currencies, not USD.
-- HAVING COUNT(*) >= 5 hides cells smaller than five people — also
-- undocumented. Semantics: docs/data_contracts.md.
--
-- Aggregates are of comp_total_raw (self-reported CompTotal in the
-- respondent's own currency). USD conversion is not implemented.

SELECT
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
    ROUND(MAX(comp_total_raw)::NUMERIC, 2) AS max_salary
FROM {{ ref('stg_survey_responses') }}
WHERE comp_total_raw IS NOT NULL
  AND comp_total_raw >= 10000
  AND comp_total_raw <= 5000000
  AND country IS NOT NULL
GROUP BY
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
ORDER BY respondent_count DESC
