# Data quality policy (raw.survey_responses)

Phase A writes **policy only**. `scripts/dq_checks.py` still logs every check
to `dwh.dq_issues` and never fails the DAG. Phase B is where this file becomes
code.

"Block publication" means: the check is a hard stop. Marts from that run must
not be treated as publishable (Phase B will fail the Airflow task / CI job).
"Log" means: write the row to `dwh.dq_issues`, emit the existing warning/info
line, and **continue**. Downstream models may still filter the same condition.

The six checks below are the six that already exist. This file does not add
new checks.

---

## `null_response_id`

**Block publication** when `row_count > 0`.

`response_id` is the only key staging can dedupe on and the only key exploded
models join on. A null key is not "missing survey data"; it is a row we cannot
attribute to a person. One such row is enough to distrust the load.

Staging currently drops `WHERE response_id IS NOT NULL`, so the marts would
silently shrink. That is recovery, not a reason to publish a load that already
failed its primary key.

---

## `duplicate_response_id`

**Log** (do not block) when `row_count > 0`.

Staging already keeps one row per `response_id` (latest `loaded_at`). The
Phase A fixture includes a duplicate on purpose; the contract is "collapse to
the newest row," not "the ZIP must be unique." Blocking publication on a
handled duplicate would fail every run the source repeats a `ResponseId`.

Log it because duplicates are still a smell (re-load bug, or a source change).
The count is **distinct ids that appear more than once**, not extra-row
volume. Review the log; do not fail the run.

If a later phase wants a threshold ("more than X% of rows are dupes"), that is
a new check, not this one.

---

## `null_country`

**Log** (do not block).

Country is optional in the survey extract. Salary and AI marts already require
`country IS NOT NULL`, so these rows never reach those published grains. A
null country is expected missingness, not evidence the CSV is corrupt.

Blocking on "any null country" would fail a normal Stack Overflow load.

---

## `null_comp_total`

**Log** (do not block).

Compensation is optional. Stack Overflow's own question text says to leave the
box empty if the respondent prefers not to answer; about two-thirds of 2024
respondents did not appear on the main salary chart. A large `null_comp_total`
count is the survey working as designed.

The salary mart already excludes nulls. Publishing tech-adoption and AI marts
must not wait for everyone to report pay.

---

## `invalid_years_code_pro`

**Block publication** when `row_count > 0`.

Staging does `CAST(years_code_pro AS NUMERIC)` for every value that is not
exactly `'Less than 1 year'` or `'More than 50 years'`. A leftover token such
as `"foo"` or `"10-11"` will **raise** in PostgreSQL and fail `dbt run` with a
cast error instead of a named quality check.

Failing here, with the check name in `dwh.dq_issues`, is clearer than failing
inside a CASE expression. Zero invalid values is the only publishable state
for this check.

`"Less than 1 year"`, `"More than 50 years"`, integers, and null are valid.
This check already encodes that.

---

## `total_rows_loaded`

**Log** the count always. **Block publication** only when `row_count = 0`.

This check is an audit total, not an issue count. A healthy production load is
tens of thousands of rows; a healthy fixture load is a dozen. There is **no**
documented "must be ~65,000" rule in this repo, and adding one would make the
Phase A fixture illegal.

Zero rows means ingest wrote nothing. There is nothing to publish.

A non-zero count is information (did this week's load shrink?). It is not a
fail.

---

## Summary

| Check | On this run, block publication? |
| --- | --- |
| `null_response_id` | Yes, if count > 0 |
| `duplicate_response_id` | No; log only |
| `null_country` | No; log only |
| `null_comp_total` | No; log only |
| `invalid_years_code_pro` | Yes, if count > 0 |
| `total_rows_loaded` | Only if count = 0; otherwise log the audit total |
