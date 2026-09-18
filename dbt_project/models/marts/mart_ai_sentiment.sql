-- AI sentiment cell: country × role × three AI answers, tagged with
-- release_id and survey_year.
--
-- Readers use marts.v_ai_sentiment. Physical table is append-only per run.
--
-- 2023 has no AIThreat / JobSat columns in the public extract. Those land
-- as NULL. pct_see_ai_as_threat is NULL when the cell has zero non-null
-- ai_threat answers — we do not report 0.0 as if nobody saw AI as a threat.
-- avg_job_satisfaction is already NULL when job_sat is non-numeric / missing.
-- See docs/data_contracts.md (per-year coverage).

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
    dev_type,
    ai_select,
    ai_sent,
    ai_threat,
    COUNT(*) AS respondent_count,
    ROUND(AVG(CASE WHEN job_sat ~ '^[0-9]+$' THEN job_sat::NUMERIC ELSE NULL END)::NUMERIC, 2) AS avg_job_satisfaction,
    CASE
        WHEN COUNT(ai_threat) = 0 THEN NULL
        ELSE ROUND(
            100.0 * SUM(CASE WHEN ai_threat ILIKE '%Yes%' THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0),
            1
        )
    END AS pct_see_ai_as_threat
FROM {{ ref('stg_survey_responses') }}
WHERE survey_year = {{ var('survey_year') | int }}
  AND (ai_select IS NOT NULL OR ai_sent IS NOT NULL)
  AND country IS NOT NULL
GROUP BY survey_year, country, dev_type, ai_select, ai_sent, ai_threat
HAVING COUNT(*) >= 3
