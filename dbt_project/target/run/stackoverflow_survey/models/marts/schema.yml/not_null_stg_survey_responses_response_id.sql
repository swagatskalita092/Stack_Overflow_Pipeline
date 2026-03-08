select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select response_id
from "survey_db"."dwh_staging"."stg_survey_responses"
where response_id is null



      
    ) dbt_internal_test