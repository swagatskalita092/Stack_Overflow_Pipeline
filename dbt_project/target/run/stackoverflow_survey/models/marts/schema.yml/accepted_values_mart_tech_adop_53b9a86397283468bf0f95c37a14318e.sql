select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    

with all_values as (

    select
        tech_type as value_field,
        count(*) as n_records

    from "survey_db"."dwh_marts"."mart_tech_adoption"
    group by tech_type

)

select *
from all_values
where value_field not in (
    'Language','Database'
)



      
    ) dbt_internal_test