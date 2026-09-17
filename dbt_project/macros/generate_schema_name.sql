{# Use the custom schema as-is (marts, staging, …) instead of dwh_marts.
   The reader views are contracted as marts.v_salary_analytics. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
