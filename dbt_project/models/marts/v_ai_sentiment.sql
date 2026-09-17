-- Live AI-sentiment mart: one published release, never a mix of two runs.

{{ config(materialized='view') }}

SELECT *
FROM {{ ref('mart_ai_sentiment') }}
WHERE release_id = (SELECT release_id FROM dwh.active_release)
