select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select language
from "survey_db"."dwh_intermediate"."int_languages_exploded"
where language is null



      
    ) dbt_internal_test