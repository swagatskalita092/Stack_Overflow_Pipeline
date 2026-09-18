-- Live salary mart: each survey_year's currently published release.
--
-- This view is the reader contract. Querying mart_salary_analytics directly
-- would mix every historical run. Join on both keys so 2023 and 2024 can
-- be live at once; a 2023 publish must not hide 2024's active rows.

{{ config(materialized='view') }}

SELECT m.*
FROM {{ ref('mart_salary_analytics') }} m
JOIN dwh.active_release ar
  ON ar.survey_year = m.survey_year
 AND ar.release_id = m.release_id
