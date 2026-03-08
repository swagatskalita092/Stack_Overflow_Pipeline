SELECT
    s.response_id,
    s.country,
    s.years_code_pro,
    s.comp_total_usd,
    s.dev_type,
    TRIM(lang.elem) AS language,
    (
        s.language_want_work IS NOT NULL
        AND s.language_want_work ILIKE '%' || TRIM(lang.elem) || '%'
    ) AS wants_to_continue
FROM "survey_db"."dwh_staging"."stg_survey_responses" s,
     LATERAL UNNEST(STRING_TO_ARRAY(s.language_have_worked, ';')) AS lang(elem)
WHERE s.language_have_worked IS NOT NULL
  AND TRIM(lang.elem) IS NOT NULL
  AND TRIM(lang.elem) != ''