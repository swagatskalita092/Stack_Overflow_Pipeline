
    
    

select
    response_id as unique_field,
    count(*) as n_records

from "survey_db"."dwh_staging"."stg_survey_responses"
where response_id is not null
group by response_id
having count(*) > 1


