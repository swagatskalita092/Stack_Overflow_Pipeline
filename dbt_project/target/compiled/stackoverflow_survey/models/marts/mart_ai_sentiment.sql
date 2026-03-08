SELECT
    country,
    dev_type,
    ai_select,
    ai_sent,
    ai_threat,
    COUNT(*) AS respondent_count,
    ROUND(AVG(CASE WHEN job_sat ~ '^[0-9]+$' THEN job_sat::NUMERIC ELSE NULL END)::NUMERIC, 2) AS avg_job_satisfaction,
    ROUND(100.0 * SUM(CASE WHEN ai_threat ILIKE '%Yes%' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS pct_see_ai_as_threat
FROM "survey_db"."dwh_staging"."stg_survey_responses"
WHERE (ai_select IS NOT NULL OR ai_sent IS NOT NULL)
  AND country IS NOT NULL
GROUP BY country, dev_type, ai_select, ai_sent, ai_threat
HAVING COUNT(*) >= 3
ORDER BY respondent_count DESC