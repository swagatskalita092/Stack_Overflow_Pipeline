select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select database_name
from "survey_db"."dwh_intermediate"."int_databases_exploded"
where database_name is null



      
    ) dbt_internal_test