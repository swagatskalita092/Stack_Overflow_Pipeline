-- AI sentiment cell: country × role × three AI answers, tagged with release_id.
--
-- Readers use marts.v_ai_sentiment. Physical table is append-only per run.

{% if var('release_id', none) is none %}
  {{ exceptions.raise_compiler_error('release_id var is required; the DAG must pass --vars') }}
{% endif %}

{{ config(
    materialized='incremental',
    incremental_strategy='append',
    full_refresh=false,
    pre_hook="{% if is_incremental() %}DELETE FROM {{ this }} WHERE release_id = '{{ var('release_id') }}'::uuid{% endif %}"
) }}

SELECT
    '{{ var("release_id") }}'::uuid AS release_id,
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
