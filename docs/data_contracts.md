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

Evidence — the entire `FROM` / filter / `GROUP BY` of
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

**Grain:** one row per
`(country, experience_band, dev_type, remote_work, org_size)`.

That is a **cell of people who share those five attributes**, not one row per
person and not one row per language/database.

**Eligible population (the rows that feed `COUNT` / `AVG` / percentiles):**

- Source: `stg_survey_responses` (already one row per `response_id`).
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

**Grain:** one row per `(tech_type, tech_name)`, where `tech_type` is
`'Language'` or `'Database'`.

Languages and databases are unioned, never mixed into one rank list.
`usage_rank` is `RANK()` within `tech_type` by `total_users` descending (ties
share a rank; the next rank skips).

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
| `want_to_continue_count` | `SUM(CASE WHEN wants_to_continue THEN 1 ELSE 0 END)` — **not** distinct. Duplicate tokens in one person's semicolon list would inflate this numerator. |
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

**Grain:** one row per `(country, dev_type, ai_select, ai_sent, ai_threat)`.

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
| `avg_job_satisfaction` | `AVG` of `job_sat` when `job_sat` matches `^[0-9]+$`. Non-numeric satisfaction is skipped in the average, **not** removed from `respondent_count`. |
| `pct_see_ai_as_threat` | `100.0 * (rows whose `ai_threat` `ILIKE '%Yes%') / COUNT(*)`. Denominator is the cell headcount, including people with null `ai_threat`. An explicit `"No"` is a zero in the numerator, not a missing row. |

**Existing filters, and whether the repo documented a reason:**

| Filter | Why it exists |
| --- | --- |
| `ai_select IS NOT NULL OR ai_sent IS NOT NULL` | **Partly obvious, partly undocumented.** Keeps people who answered at least one of those two AI questions. No repo note explains excluding `ai_threat`-only rows. |
| `country IS NOT NULL` | Same as salary: grouping key. |
| `HAVING COUNT(*) >= 3` | **No documented reason in this repo.** Lower than salary's five; no write-up why. |

---

## Upstream grain (needed to read the marts)

**`stg_survey_responses`:** one row per `response_id`. Raw duplicates keep the
row with the latest `loaded_at` (`DISTINCT ON (response_id) ... ORDER BY
response_id, loaded_at DESC NULLS LAST`). `"Less than 1 year"` → `0`;
`"More than 50 years"` → `51`.

**`int_languages_exploded`:** one row per `(response_id, language token)`.
Carries `comp_total_raw` along for convenience; that column is **not** a
permission to aggregate salary from this table.

**`int_databases_exploded`:** one row per `(response_id, database token)`.
Does **not** carry `comp_total_raw` (inconsistent with languages; unused by
salary).
