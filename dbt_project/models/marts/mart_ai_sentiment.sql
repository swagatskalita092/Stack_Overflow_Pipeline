-- AI sentiment cell: country × role × three AI answers.
--
-- Grain is that combination, not "developers in France." A person who skipped
-- every AI question is out (fixture R012). A person who said "No, and I don't
-- plan to" is in if their cell reaches three people (fixtures R009–R011).
-- Skipping the question and answering No are not the same.
--
-- WHERE requires ai_select or ai_sent. ai_threat alone is not enough — that
-- drop is undocumented. HAVING COUNT(*) >= 3 is an undocumented small-cell
-- rule (different from salary's five).
--
-- pct_see_ai_as_threat denominator is the cell headcount, including people
-- whose ai_threat is null. Numerator is ILIKE '%Yes%'. Explicit "No" is a
-- zero, not a missing row. avg_job_satisfaction ignores non-numeric job_sat
-- but still counts those people in respondent_count.

SELECT
    country,
    dev_type,
    ai_select,
    ai_sent,
    ai_threat,
    COUNT(*) AS respondent_count,
    ROUND(AVG(CASE WHEN job_sat ~ '^[0-9]+$' THEN job_sat::NUMERIC ELSE NULL END)::NUMERIC, 2) AS avg_job_satisfaction,
    ROUND(100.0 * SUM(CASE WHEN ai_threat ILIKE '%Yes%' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS pct_see_ai_as_threat
FROM {{ ref('stg_survey_responses') }}
WHERE (ai_select IS NOT NULL OR ai_sent IS NOT NULL)
  AND country IS NOT NULL
GROUP BY country, dev_type, ai_select, ai_sent, ai_threat
HAVING COUNT(*) >= 3
ORDER BY respondent_count DESC
