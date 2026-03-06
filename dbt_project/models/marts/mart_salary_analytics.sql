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
    ROUND(AVG(comp_total_usd)::NUMERIC, 2) AS avg_salary_usd,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY comp_total_usd))::NUMERIC, 2) AS median_salary_usd,
    ROUND((PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY comp_total_usd))::NUMERIC, 2) AS p25_salary_usd,
    ROUND((PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY comp_total_usd))::NUMERIC, 2) AS p75_salary_usd,
    ROUND(MIN(comp_total_usd)::NUMERIC, 2) AS min_salary_usd,
    ROUND(MAX(comp_total_usd)::NUMERIC, 2) AS max_salary_usd
FROM {{ ref('stg_survey_responses') }}
WHERE comp_total_usd IS NOT NULL
  AND comp_total_usd >= 10000
  AND comp_total_usd <= 5000000
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
