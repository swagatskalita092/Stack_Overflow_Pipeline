-- One row per (respondent, language they have worked with).
--
-- Why explode: mart_tech_adoption needs to count Python users, not people
-- whose semicolon list happens to contain the letters P-y-t-h-o-n. The
-- survey stores LanguageHaveWorkedWith as "Python;SQL;Go".
--
-- Grain: (response_id, language). R006 (three languages) becomes three rows.
-- comp_total_raw is copied onto each row as a convenience. Do **not** SUM or
-- AVG that column from this table — that is the fan-out bug
-- mart_salary_analytics avoids by reading stg_survey_responses instead.
--
-- wants_to_continue: true when language_want_work contains the token as an
-- ILIKE substring. "Java" will also match "JavaScript". That is current
-- behavior, documented in docs/data_contracts.md, not a join.

SELECT
    s.survey_year,
    s.response_id,
    s.country,
    s.years_code_pro,
    s.comp_total_raw,
    s.dev_type,
    TRIM(lang.elem) AS language,
    (
        s.language_want_work IS NOT NULL
        AND s.language_want_work ILIKE '%' || TRIM(lang.elem) || '%'
    ) AS wants_to_continue
FROM {{ ref('stg_survey_responses') }} s,
     LATERAL UNNEST(STRING_TO_ARRAY(s.language_have_worked, ';')) AS lang(elem)
WHERE s.language_have_worked IS NOT NULL
  AND TRIM(lang.elem) IS NOT NULL
  AND TRIM(lang.elem) != ''
