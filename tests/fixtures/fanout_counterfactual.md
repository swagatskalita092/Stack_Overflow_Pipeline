# Counterfactual: the join that would inflate salary (not in current SQL)

`mart_salary_analytics` does **not** do this. This file exists so the Part 1
answer is falsifiable: if someone later joins the exploded tables into the
salary aggregation, these three people are enough to see it.

## Tiny respondents

| response_id | salary | languages | databases |
| --- | --- | --- | --- |
| A | 100000 | Python;Java | PostgreSQL;MySQL |
| B | 200000 | Python | (none) |
| C | 300000 | (none) | MySQL |

Ignore country filters and `HAVING >= 5` for this sketch. One row per person
in staging.

## Correct aggregation (what the current mart does)

```text
FROM stg_survey_responses
```

Three rows. `COUNT(*) = 3`. `AVG(salary) = (100+200+300)/3 = 200000`.
Person A counts once despite two languages and two databases.

## Inflating aggregation (not shipped)

```text
FROM stg_survey_responses s
LEFT JOIN int_languages_exploded l ON l.response_id = s.response_id
LEFT JOIN int_databases_exploded d ON d.response_id = s.response_id
```

Cross of A's exploded rows: 2 languages × 2 databases = **4 copies of 100000**.

B: 1 language × 1 null-database left join = **1 copy of 200000**.

C: 1 null-language left join × 1 database = **1 copy of 300000**.

`COUNT(*) = 6`. `AVG` = (100000×4 + 200000 + 300000) / 6 = **150000**.

Person A's pay is quadruple-weighted. Median and percentiles move with it.
That is the bug the current `FROM {{ ref('stg_survey_responses') }}` avoids.

Joining **only** languages would triple-count anyone with three languages
(see fixture `R006`). Joining **only** databases would double-count `R006`.
Joining both multiplies the two fan-outs.
