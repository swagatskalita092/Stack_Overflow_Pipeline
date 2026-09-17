# Hand-calculated fixtures (Phase A)

These files are the **ground truth** for Phase C CI. Numbers were computed
with pencil logic from the SQL contracts in `docs/data_contracts.md`, not by
running dbt or copying warehouse output.

Load `raw_survey_responses.csv` into `raw.survey_responses` (13 rows, 12
distinct `response_id`s). After staging, expect 12 rows. Compare marts to the
`expected_*.csv` files. Empty CSV cells are SQL NULL.


---

## What each respondent is for

| id | Why they exist |
| --- | --- |
| R001–R005 | Shared salary **and** AI cell (need ≥5 for salary, ≥3 for AI). |
| R006 | Fan-out: 3 languages × 2 databases, **one** salary. Same salary cell as R001–R005 so a bad join would move avg/median/count. |
| R007 | Missing salary. Out of salary mart; still in the US AI cell. |
| R008 | Duplicate `response_id`. Old row would pollute the US salary cell (`999999`); newest row is India and must win. |
| R009 | `"Less than 1 year"` → staged `0` → band `0-1 years`. Also one of three explicit-AI-No Canadians. |
| R010 | `"More than 50 years"` → staged `51` → band `20+ years`. Salary `6000000` fails the 5M cap. Same AI-No cell as R009. |
| R011 | Null experience → band `Unknown`. Explicit AI `"No"`, not a skip. |
| R012 | AI non-response (all three AI fields empty). Different from R011's explicit No. |

R009–R011 share `(Canada, Developer, back-end, No-and-don't-plan, Unfavorable, No)` so the AI mart can emit an explicit-No row. They do **not** share a salary cell (different experience / salary filters).

---

## Salary mart (hand calc)

Eligible for `WHERE`: R001–R006 (US, 100k–150k, country present).

Out:

- R007 — null compensation
- R008 old — discarded at staging
- R008 new — India, cell size 1 (`HAVING >= 5`)
- R009 — Canada, `0-1 years`, cell size 1
- R010 — `6000000` > 5M
- R011 — Canada / Unknown, cell size 1
- R012 — France / 2-4 years, cell size 1

If R008's **old** row survived, the US cell would have 7 people and `max_salary = 999999`. Expected max is **150000**.

If R006 were joined to exploded languages **and** databases, that one person would appear 3 × 2 = **6** times in the US cell. Expected `respondent_count` is **6** (R001–R006 once each), not 11 (5 + 6 copies of R006).

US cell attributes: `United States`, `5-9 years` (pro years = 7), `Developer, full-stack`, `Remote`, `100 to 499 employees`.

Salaries in order: 100000, 110000, 120000, 130000, 140000, 150000. N = 6.

- `respondent_count` = 6
- `avg` = (100000+110000+120000+130000+140000+150000) / 6 = **125000.00**
- `PERCENTILE_CONT` position = `1 + p * (N - 1)`
  - median p=0.5 → position 3.5 → halfway 120000 and 130000 = **125000.00**
  - p25 → position 2.25 → 110000 + 0.25×(120000−110000) = **112500.00**
  - p75 → position 4.75 → 130000 + 0.75×(140000−130000) = **137500.00**
- min **100000.00**, max **150000.00**

That single row is `expected_mart_salary_analytics.csv`.

---

## Tech-adoption mart (hand calc)

Published SQL keeps `total_users >= 100`. This fixture has 12 people, so
**`expected_mart_tech_adoption.csv` is headers only** (zero rows). That is
intentional: CI should prove the cutoff, not relax it.

Pre-threshold counts (for `expected_mart_tech_adoption_pre_threshold.csv`)
use `COUNT(DISTINCT response_id)` and the `ILIKE` continue flag as written.

Languages (`have` / `want` → continue?):

| Person | Have | Want | Continue |
| --- | --- | --- | --- |
| R001 | Python | Python | Python yes |
| R002 | Python;JavaScript | Python | Python yes, JS no |
| R003 | Python;SQL | Python;SQL | both yes |
| R004 | JavaScript | JavaScript | JS yes |
| R005 | Python;Java | Java | Python no, Java yes |
| R006 | Python;JavaScript;Go | Python;Go | Python yes, JS no, Go yes |
| R007 | Python | (empty) | Python no |
| R008 | Java | Java | Java yes (newest row only) |
| R009 | Python | Python | Python yes |
| R010 | C | (empty) | C no |
| R011, R012 | (empty) |  | no language rows |

| Tech | users | want_to_continue | retention |
| --- | --- | --- | --- |
| Python | R001,R002,R003,R005,R006,R007,R009 = 7 | R001,R002,R003,R006,R009 = 5 | 100×5/7 = **71.4** |
| JavaScript | R002,R004,R006 = 3 | R004 = 1 | **33.3** |
| Java | R005,R008 = 2 | 2 | **100.0** |
| SQL | R003 = 1 | 1 | **100.0** |
| Go | R006 = 1 | 1 | **100.0** |
| C | R010 = 1 | 0 | **0.0** |

`RANK()` by users desc: Python 1, JavaScript 2, Java 3, SQL/Go/C tied at 4.

Databases:

| Person | Have | Want | Continue |
| --- | --- | --- | --- |
| R001 | PostgreSQL | PostgreSQL | yes |
| R002 | PostgreSQL | PostgreSQL | yes |
| R003 | PostgreSQL;MySQL | PostgreSQL | PG yes, MySQL no |
| R004 | MySQL | MySQL | yes |
| R005 | PostgreSQL;Redis | Redis | PG no, Redis yes |
| R006 | PostgreSQL;MongoDB | PostgreSQL | PG yes, Mongo no |
| R007 | PostgreSQL | PostgreSQL | yes |

| Tech | users | want_to_continue | retention |
| --- | --- | --- | --- |
| PostgreSQL | R001,R002,R003,R005,R006,R007 = 6 | 5 (not R005) | 100×5/6 = **83.3** |
| MySQL | R003,R004 = 2 | 1 (R004) | **50.0** |
| Redis | R005 = 1 | 1 | **100.0** |
| MongoDB | R006 = 1 | 0 | **0.0** |

Ranks: PostgreSQL 1, MySQL 2, Redis/MongoDB tied at 3.

The published mart SQL is `ORDER BY tech_type, usage_rank` with no `tech_name`
tie-break, so MongoDB vs Redis (and C vs Go vs SQL) order is not guaranteed.
`expected_mart_tech_adoption_pre_threshold.csv` lists ties alphabetically for
humans; Phase C should compare those rows as a set.


---

## AI mart (hand calc)

`WHERE (ai_select IS NOT NULL OR ai_sent IS NOT NULL)` and `HAVING COUNT(*) >= 3`.

**Cell A** — United States / Developer, full-stack / Yes / Favorable / No
People: R001–R007 (R007 has no salary; still answered AI). Count = **7**.
`job_sat`: 8,7,8,9,8,8,8. Average = 56/7 = **8.00**.
`ai_threat` is `"No"` for all → `ILIKE '%Yes%'` hits 0 → **0.0**.

**Cell B** — Canada / Developer, back-end / `No, and I don't plan to` / Unfavorable / No
People: R009, R010, R011. Count = **3**.
`job_sat`: 6, 5, 4. Average = **5.00**.
Threat `"No"` → **0.0**.

Out:

- R008 newest — India cell of 1
- R012 — all AI fields empty (non-response). This is the pair against Cell B: skipping the question is not an explicit `"No"`.

Order in the mart: `ORDER BY respondent_count DESC` → Cell A then Cell B.

---

## Staging notes used in expected CSVs

- R008 keeps `loaded_at = 2024-06-20 12:00:00` (India, 180000, Java). The
  2024-01-15 row is gone.
- R009 `years_code` and `years_code_pro` → `0`.
- R010 both → `51`. `comp_total_raw` stays `6000000` in staging; the salary
  mart drops it later.
- R011 `years_code_pro` stays null.
- R008 `currency = INR` with `comp_total_raw = 180000`: the value is not
  converted to USD (conversion is not implemented).
