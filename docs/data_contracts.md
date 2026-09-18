# Mart data contracts

This file is the Phase A contract for the three published marts. It describes
**grain**, **who is in the table**, **what a percentage is out of**, and **every
filter that already exists in SQL**. Where the repo never wrote down *why* a
cutoff exists, this file says so instead of inventing a requirement.

The only Phase A SQL change besides comments is renaming the dishonest
`comp_total_usd` / `*_salary_usd` fields to `comp_total_raw` / `avg_salary`
(and the other salary aggregates without `_usd`). Grain and filters are
unchanged.

---

## Shared: what `comp_total_raw` is (and is not)

`comp_total_raw` is **self-reported survey compensation** from Stack Overflow's
respondent pool, stored as a number in **whatever currency the respondent
typed**. It is **not** USD, and it is **not** a representative labor-market
salary sample.

USD conversion is **not implemented**. Stack Overflow converts local currencies
to USD using the exchange rate on **11 June 2024**; this pipeline only strips
non-numeric characters from `comp_total` and casts. The `currency` column is
stored on staging and then ignored. A later phase can add FX; until then the
column name is `comp_total_raw` on purpose.

Stack Overflow's own 2024 methodology states that respondents were recruited
primarily through channels Stack Overflow owns (onsite messaging, blog posts,
email/newsletters, banner ads, social media), so **highly-engaged Stack Overflow
users were more likely to see the survey**. The published results use 65,437
responses from 185 countries after qualifier questions (attention check, consent,
and "which option best describes you today?"); about 20,000 further responses
were not used. See [Methodology | 2024 Stack Overflow Developer Survey](https://survey.stackoverflow.co/2024/methodology/).

The compensation question itself is optional and self-entered: *"What is your
current total annual compensation (salary, bonuses, and perks, before taxes and
deductions)?"* Respondents who prefer not to answer leave it empty. Stack
Overflow reports 22,677 responses (34.7%) on the main salary-by-developer-type
chart. See [Work | 2024 Stack Overflow Developer Survey](https://survey.stackoverflow.co/2024/work).

A second pipeline caveat sits on top of that survey design: **no survey
weights**. Every eligible respondent counts as one row. There is no raking, no
non-response adjustment, and no attempt to match BLS / OECD occupational totals.

Read every salary number in `mart_salary_analytics` as: *among people who took
this survey, passed our numeric filters, and landed in this cell, in mixed
local currencies*. Not: *the typical developer in this country/role, in USD*.

---

## Fan-out investigation (Phase A, Part 1)

**Question:** can `mart_salary_analytics` double- or triple-count a respondent's
compensation because `int_languages_exploded` and/or `int_databases_exploded`
are joined into the salary aggregation?

**Answer: no. The salary mart does not join those tables at all.** One
respondent with three languages and two databases still contributes **one**
salary to `AVG` / `PERCENTILE_CONT` / `COUNT(*)`.

Evidence: the entire `FROM` / filter / `GROUP BY` of
`dbt_project/models/marts/mart_salary_analytics.sql`:

```sql
FROM {{ ref('stg_survey_responses') }}
WHERE comp_total_raw IS NOT NULL
  AND comp_total_raw >= 10000
  AND comp_total_raw <= 5000000
  AND country IS NOT NULL
GROUP BY
    country,
    CASE ... END,   -- experience_band
    dev_type,
    remote_work,
    org_size
HAVING COUNT(*) >= 5
```

There is no `JOIN`, no `int_languages_exploded`, and no `int_databases_exploded`.

Those exploded models are used only by `mart_tech_adoption` (each in its own
CTE, never crossed with each other, never crossed with salary):

```sql
-- mart_tech_adoption.sql
FROM {{ ref('int_languages_exploded') }}   -- lang_stats CTE only
FROM {{ ref('int_databases_exploded') }}   -- db_stats CTE only
```

`int_languages_exploded` **does** copy `comp_total_raw` onto every
`(response_id, language)` row. That is a landmine if a later salary-by-language
mart joins it without collapsing back to one row per respondent first. It is not
a bug in the current salary mart, because that mart never reads the exploded
model.

A 2–3 person illustration of the join that would inflate numbers (the join
**is not in the current SQL**) lives in
`tests/fixtures/fanout_counterfactual.md`. Fixture respondent `R006` is the
regression net: three languages × two databases, still one salary.

---

## `mart_salary_analytics`

**Grain (one published answer):** one row per
`(survey_year, country, experience_band, dev_type, remote_work, org_size)`.

**Physical table grain:** the same keys plus `release_id`. Every pipeline run
appends a new copy for the year it was opened for. Analysts must query
`marts.v_salary_analytics`, which joins `dwh.active_release` on
`(survey_year, release_id)` so 2023 and 2024 can both be live. Querying the
table directly will mix history.

That is a **cell of people who share those five attributes**, not one row per
person and not one row per language/database.

**Eligible population (the rows that feed `COUNT` / `AVG` / percentiles):**

- Source: `stg_survey_responses` (already one row per `(survey_year, response_id)`).
- `survey_year` equals this run's `--vars` year (a 2023 run does not rewrite 2024 cells under the new `release_id`).
- `comp_total_raw IS NOT NULL`
- `comp_total_raw >= 10000`
- `comp_total_raw <= 5000000`
- `country IS NOT NULL`
- After grouping: `HAVING COUNT(*) >= 5`

A respondent with a valid salary in a cell of four people is **dropped**. They
were eligible for the `WHERE`, then lost at `HAVING`.

**Denominator for percentages:** this mart has no percentage column.
`respondent_count` is `COUNT(*)` of eligible rows in the cell. Because the
source is one row per respondent and there is no exploded join, that count is
a headcount, not a technology mention count.

**Experience bands** (applied to staged numeric `years_code_pro`):

| Band | Rule |
| --- | --- |
| Unknown | `years_code_pro IS NULL` |
| 0-1 years | `< 2` (includes staging's `0` from `"Less than 1 year"`) |
| 2-4 years | `< 5` |
| 5-9 years | `< 10` |
| 10-19 years | `< 20` |
| 20+ years | everything else (includes staging's `51` from `"More than 50 years"`) |

Boundaries: `2` is 2-4, `5` is 5-9, `10` is 10-19, `20` is 20+.

**Existing filters, and whether the repo documented a reason:**

| Filter | Why it exists |
| --- | --- |
| `comp_total_raw IS NOT NULL` | Compensation is optional. A null cannot go into `AVG` / percentiles. |
| `>= 10000` and `<= 5000000` | **No documented reason in this repo.** Likely a heuristic to drop empty/token amounts and joke outliers. Stack Overflow's published salary charts do not state this band. |
| `country IS NOT NULL` | **No documented reason beyond "we group by country."** Null country would collapse into one unlabeled cell. |
| `HAVING COUNT(*) >= 5` | **No documented reason in this repo.** Likely to hide tiny cells. Five is a choice, not a survey rule. |

**Percentiles:** PostgreSQL `PERCENTILE_CONT` (continuous, interpolated). For
N sorted values the p-th percentile sits at position `1 + p * (N - 1)`.

---

## `mart_tech_adoption`

**Grain:** one row per `(survey_year, tech_type, tech_name)`, where `tech_type` is
`'Language'` or `'Database'`.

Languages and databases are unioned, never mixed into one rank list.
`usage_rank` is `RANK()` within `(survey_year, tech_type)` by `total_users`
descending (ties share a rank; the next rank skips).

**Eligible population:**

- Languages: `int_languages_exploded`, which is `stg_survey_responses` unnested
  on `language_have_worked` where that field is not null and the token is not
  blank. People who listed no language do not appear.
- Databases: the same pattern on `database_have_worked`.
- After aggregating: `WHERE total_users >= 100`.

**Denominators:**

| Column | Meaning |
| --- | --- |
| `total_users` | `COUNT(DISTINCT response_id)` who listed that tech. One person with `Python;SQL` counts once for Python and once for SQL, never twice for Python. |
| `want_to_continue_count` | `SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END)`: **not** distinct. Duplicate tokens in one person's semicolon list would inflate this numerator. |
| `retention_rate_pct` | `100.0 * want_to_continue_count / total_users`, rounded to 1 decimal. Denominator is distinct users of **that tech**, not all survey respondents and not all people who answered any tech question. |

`wants_to_continue` is true when `language_want_work` / `database_want_work`
contains the token as an `ILIKE '%token%'` substring. That is **not** a strict
semicolon membership test. `"Java"` matches `"JavaScript"`. `"C"` matches
`"C++"`. This file records the behavior; Phase A does not change it.

**Existing filters, and whether the repo documented a reason:**

| Filter | Why it exists |
| --- | --- |
| Non-null, non-blank exploded tokens | Empty splits would create fake tech names. |
| `total_users >= 100` | **No documented reason in this repo.** Likely to drop rare tools. One hundred is a choice. On the ~12-row Phase A fixture this filter makes the **published mart empty**; the pre-threshold counts are in `tests/fixtures/expected_mart_tech_adoption_pre_threshold.csv`. |

---

## `mart_ai_sentiment`

**Grain:** one row per `(survey_year, country, dev_type, ai_select, ai_sent, ai_threat)`.

That is a **combination of three AI answers**, not "people in this country who
use AI." `dev_type` is the raw survey string (often a single primary role in
this extract, not exploded).

**Eligible population:**

- Source: `stg_survey_responses`.
- `(ai_select IS NOT NULL OR ai_sent IS NOT NULL)`
- `country IS NOT NULL`
- After grouping: `HAVING COUNT(*) >= 3`

Non-response (all of `ai_select` / `ai_sent` / `ai_threat` empty) is **out**.
An explicit `"No"` on `ai_select` or `ai_threat` is **in**, if the row also
passes the `ai_select OR ai_sent` predicate and the cell reaches three people.

A respondent who answered **only** `ai_threat` (both `ai_select` and `ai_sent`
null) is currently **dropped**. The SQL never says why `ai_threat` alone is
not enough.

**Denominators:**

| Column | Meaning |
| --- | --- |
| `respondent_count` | `COUNT(*)` in the cell (one staged row per person). |
| `avg_job_satisfaction` | `AVG` of `job_sat` when `job_sat` matches `^[0-9]+(\.[0-9]+)?$` (integer or decimal text such as `8` or `8.0`). Non-numeric / missing satisfaction is skipped in the average, **not** removed from `respondent_count`. **Bug, found Phase F (2024-09-18) while rendering the dashboard from a real 2024 CDN publish:** the previous gate was `^[0-9]+$`, which rejected every 2024 answer (`'8.0'`, `'7.0'`, …). 29,126 raw 2024 rows had `job_sat`; 0 of 3,828 published AI cells got a non-null average. The dashboard would have labeled that NULL as “Not asked in 2024,” which is false: JobSat was on the 2024 survey. Fixed in `mart_ai_sentiment.sql` the same day. 2023 remains all-NULL because the extract has no `JobSat` column. |
| `pct_see_ai_as_threat` | For years where `ai_threat` exists (2024): `100.0 * (rows whose `ai_threat` `ILIKE '%Yes%') / COUNT(*)`. Denominator is the cell headcount, including people with null `ai_threat`. An explicit `"No"` is a zero in the numerator, not a missing row. **When the cell has zero non-null `ai_threat` values (all of 2023), this column is NULL.** We do not report `0.0`. Zero would mean "nobody in this cell sees AI as a threat," which is a lie when the question was not on the survey. |

**Existing filters, and whether the repo documented a reason:**

| Filter | Why it exists |
| --- | --- |
| `ai_select IS NOT NULL OR ai_sent IS NOT NULL` | **Partly obvious, partly undocumented.** Keeps people who answered at least one of those two AI questions. No repo note explains excluding `ai_threat`-only rows. |
| `country IS NOT NULL` | Same as salary: grouping key. |
| `HAVING COUNT(*) >= 3` | **No documented reason in this repo.** Lower than salary's five; no write-up why. |

---

## Per-year column coverage (Phase E)

`survey_year` is an explicit ingest stamp (`--year` / `SURVEY_YEAR`), copied
from `raw.survey_responses` through staging → intermediate → marts. It is not
inferred from a filename.

The 2023 public extract is missing **AIThreat** and **JobSat**. Ingest warns
and lands `ai_threat` / `job_sat` as NULL for every 2023 row. That is a known
schema gap (`dq_checks.KNOWN_ABSENT_COLUMNS[2023]`), not a data-quality
failure: `run_dq_checks` does not have a generic "column is all NULL" rule,
and must not grow one that flags 2023 without consulting that allowlist.

2023 rows still enter `mart_ai_sentiment` when they have `ai_select` or
`ai_sent` (those columns exist in 2023). The AI-threat *rate* and the job-
satisfaction *average* are the things we refuse to invent.

| Mart column | 2023 | 2024 | Notes |
| --- | --- | --- | --- |
| `mart_salary_analytics.survey_year` | yes | yes | Copied from raw. |
| `mart_salary_analytics.country` | yes | yes | |
| `mart_salary_analytics.experience_band` | yes | yes | From `years_code_pro` (present both years). |
| `mart_salary_analytics.dev_type` | yes | yes | |
| `mart_salary_analytics.remote_work` | yes | yes | |
| `mart_salary_analytics.org_size` | yes | yes | |
| `mart_salary_analytics.respondent_count` | yes | yes | |
| `mart_salary_analytics.avg_salary` | yes | yes | `comp_total_raw`; mixed local currencies. |
| `mart_salary_analytics.median_salary` | yes | yes | |
| `mart_salary_analytics.p25_salary` | yes | yes | |
| `mart_salary_analytics.p75_salary` | yes | yes | |
| `mart_salary_analytics.min_salary` | yes | yes | |
| `mart_salary_analytics.max_salary` | yes | yes | |
| `mart_tech_adoption.survey_year` | yes | yes | Rank is per year, not global. |
| `mart_tech_adoption.tech_name` | yes | yes | |
| `mart_tech_adoption.total_users` | yes | yes | |
| `mart_tech_adoption.want_to_continue_count` | yes | yes | |
| `mart_tech_adoption.retention_rate_pct` | yes | yes | |
| `mart_tech_adoption.tech_type` | yes | yes | |
| `mart_tech_adoption.usage_rank` | yes | yes | `PARTITION BY survey_year, tech_type`. |
| `mart_ai_sentiment.survey_year` | yes | yes | |
| `mart_ai_sentiment.country` | yes | yes | |
| `mart_ai_sentiment.dev_type` | yes | yes | |
| `mart_ai_sentiment.ai_select` | yes | yes | Present in both extracts. |
| `mart_ai_sentiment.ai_sent` | yes | yes | Present in both extracts. |
| `mart_ai_sentiment.ai_threat` | **NULL** | yes | 2023 extract has no `AIThreat` column. |
| `mart_ai_sentiment.respondent_count` | yes | yes | People who answered `ai_select` or `ai_sent`. |
| `mart_ai_sentiment.avg_job_satisfaction` | **NULL** | yes | 2023 extract has no `JobSat` column; AVG of no numeric values is NULL. 2024 answers are decimal text (`8.0`); see the regex fix under Denominators. |
| `mart_ai_sentiment.pct_see_ai_as_threat` | **NULL** | yes | `COUNT(ai_threat) = 0` → NULL, not 0.0. See above. |

Asserted by `tests/test_year_2023.py` (ingest + DQ allowlist + dbt NULL rate)
and `tests/fixtures/expected_mart_ai_sentiment_2023.csv`.

---

## Upstream grain (needed to read the marts)

**`stg_survey_responses`:** one row per `(survey_year, response_id)`. Raw
duplicates *within a year* keep the row with the latest `loaded_at`
(`DISTINCT ON (survey_year, response_id) ... ORDER BY survey_year, response_id,
loaded_at DESC NULLS LAST`). The same `ResponseId` in 2023 and 2024 is two
rows. `"Less than 1 year"` → `0`; `"More than 50 years"` → `51`.

**`int_languages_exploded`:** one row per `(survey_year, response_id, language token)`.
Carries `comp_total_raw` along for convenience; that column is **not** a
permission to aggregate salary from this table.

**`int_databases_exploded`:** one row per `(survey_year, response_id, database token)`.
Does **not** carry `comp_total_raw` (inconsistent with languages; unused by
salary).

Raw ingest (`raw.survey_responses`) is year-scoped DELETE + reload: ingesting
2023 does not destroy 2024 raw rows. See [known_limitations.md](known_limitations.md).
