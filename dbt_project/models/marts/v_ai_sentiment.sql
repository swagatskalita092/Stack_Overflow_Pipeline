-- Live AI-sentiment mart: each survey_year's currently published release.

{{ config(materialized='view') }}

SELECT m.*
FROM {{ ref('mart_ai_sentiment') }} m
JOIN dwh.active_release ar
  ON ar.survey_year = m.survey_year
 AND ar.release_id = m.release_id
