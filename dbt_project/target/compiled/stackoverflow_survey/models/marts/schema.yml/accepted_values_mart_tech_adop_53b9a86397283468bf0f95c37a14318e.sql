
    
    

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


