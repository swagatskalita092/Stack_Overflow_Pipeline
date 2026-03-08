
    
    

with all_values as (

    select
        wants_to_continue as value_field,
        count(*) as n_records

    from "survey_db"."dwh_intermediate"."int_languages_exploded"
    group by wants_to_continue

)

select *
from all_values
where value_field not in (
    'True','False'
)


