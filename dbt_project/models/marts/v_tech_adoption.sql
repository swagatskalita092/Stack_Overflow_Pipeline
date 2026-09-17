-- Live tech-adoption mart: one published release, never a mix of two runs.

{{ config(materialized='view') }}

SELECT *
FROM {{ ref('mart_tech_adoption') }}
WHERE release_id = (SELECT release_id FROM dwh.active_release)
