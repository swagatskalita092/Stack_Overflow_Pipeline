-- Grain of staging is (survey_year, response_id), not response_id alone.
-- The same ResponseId can appear in 2023 and 2024; that is two people-years.
select survey_year, response_id
from {{ ref('stg_survey_responses') }}
group by 1, 2
having count(*) > 1
