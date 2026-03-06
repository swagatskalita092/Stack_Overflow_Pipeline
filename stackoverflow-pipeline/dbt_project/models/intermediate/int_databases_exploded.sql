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
