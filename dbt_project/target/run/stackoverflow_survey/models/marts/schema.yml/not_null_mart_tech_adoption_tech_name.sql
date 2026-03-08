select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select tech_name
from "survey_db"."dwh_marts"."mart_tech_adoption"
where tech_name is null



      
    ) dbt_internal_test