select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select respondent_count
from "survey_db"."dwh_marts"."mart_ai_sentiment"
where respondent_count is null



      
    ) dbt_internal_test