-- Live salary mart: whichever release_id sits in dwh.active_release.
--
-- This view is the reader contract. Querying mart_salary_analytics directly
-- would mix every historical run. If active_release is empty, the subquery
-- returns no row and the view is empty (nothing published yet).

{{ config(materialized='view') }}

SELECT *
FROM {{ ref('mart_salary_analytics') }}
WHERE release_id = (SELECT release_id FROM dwh.active_release)
