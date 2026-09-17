-- One row per (respondent, database they have worked with).
--
-- Same idea as int_languages_exploded, for DatabaseHaveWorkedWith. Used only
-- by mart_tech_adoption. Not joined into salary.
--
-- Grain: (response_id, database_name). Unlike the language model this one
-- does not select comp_total_raw. That inconsistency is harmless today
-- because salary does not read either exploded table.

SELECT
    s.response_id,
    s.country,
    s.dev_type,
    s.years_code_pro,
    TRIM(db.elem) AS database_name,
    (
        s.database_want_work IS NOT NULL
        AND s.database_want_work ILIKE '%' || TRIM(db.elem) || '%'
    ) AS wants_to_continue
FROM {{ ref('stg_survey_responses') }} s,
     LATERAL UNNEST(STRING_TO_ARRAY(s.database_have_worked, ';')) AS db(elem)
WHERE s.database_have_worked IS NOT NULL
  AND TRIM(db.elem) IS NOT NULL
  AND TRIM(db.elem) != ''
