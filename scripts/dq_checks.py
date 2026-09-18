"""Landing-table checks on raw.survey_responses, logged to dwh.dq_issues.

Why this script exists
----------------------
dbt tests the models. This script tests the *landing table* before dbt runs,
so a bad load shows up as a named check (null keys, dupes, empty table,
catastrophic row-count drop) instead of a mysterious CAST error three
tasks later.

docs/data_quality_policy.md is enforced. Blocking checks raise
PublicationBlocked so Airflow never reaches dbt/publish. Log-only checks
still write dwh.dq_issues and continue.

Checks are scoped to the survey_year this run is loading. A 2023 ingest
must not be compared to 2024's published headcount, and a ResponseId that
appears in both years is not a duplicate.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection
from ingest_survey import resolve_survey_year

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Warehouse columns that are expected to be all-NULL for a given year
# because the public extract does not have the source field at all.
# 2023 has no AIThreat / JobSat (verified against the real header).
# A generic "high null rate" check must skip these rather than treat a
# known schema gap as a data-quality failure. See data_contracts.md.
KNOWN_ABSENT_COLUMNS = {
    2023: frozenset({"ai_threat", "job_sat"}),
}


def known_absent_columns(survey_year: int) -> frozenset[str]:
    """Warehouse columns that are NULL for every row of this year, by design."""
    return KNOWN_ABSENT_COLUMNS.get(int(survey_year), frozenset())


def _dq_checks(survey_year: int) -> list[dict]:
    """One check dict per landing-table test, scoped to this survey_year.

    `sql` must return a single integer (a COUNT). `issue_type` AUDIT is
    "how many rows did we load?", not "how many are bad." `blocks_when` is
    a callable(row_count) -> bool from the Phase A policy.
    """
    year = int(survey_year)
    return [
        {
            "name": "null_response_id",
            "issue_type": "NULL_PRIMARY_KEY",
            "sql": (
                "SELECT COUNT(*) FROM raw.survey_responses "
                f"WHERE survey_year = {year} AND response_id IS NULL"
            ),
            "details": f"Rows with null response_id (survey_year={year})",
            "blocks_when": lambda n: n > 0,
        },
        {
            "name": "duplicate_response_id",
            "issue_type": "DUPLICATE_KEY",
            "sql": f"""
                SELECT COUNT(*) FROM (
                    SELECT response_id FROM raw.survey_responses
                    WHERE survey_year = {year}
                    GROUP BY response_id HAVING COUNT(*) > 1
                ) dup
            """,
            "details": (
                f"Distinct response_ids appearing more than once "
                f"(survey_year={year})"
            ),
            "blocks_when": lambda n: False,
        },
        {
            "name": "null_country",
            "issue_type": "MISSING_DIMENSION",
            "sql": (
                "SELECT COUNT(*) FROM raw.survey_responses "
                f"WHERE survey_year = {year} AND country IS NULL"
            ),
            "details": f"Rows with null country (survey_year={year})",
            "blocks_when": lambda n: False,
        },
        {
            "name": "null_comp_total",
            "issue_type": "MISSING_METRIC",
            "sql": (
                "SELECT COUNT(*) FROM raw.survey_responses "
                f"WHERE survey_year = {year} AND comp_total IS NULL"
            ),
            "details": f"Rows with null comp_total (survey_year={year})",
            "blocks_when": lambda n: False,
        },
        {
            "name": "invalid_years_code_pro",
            "issue_type": "FORMAT_INCONSISTENCY",
            "sql": f"""
                SELECT COUNT(*) FROM raw.survey_responses
                WHERE survey_year = {year}
                  AND years_code_pro IS NOT NULL
                  AND years_code_pro NOT IN ('Less than 1 year', 'More than 50 years')
                  AND years_code_pro ~ '[^0-9]'
            """,
            "details": (
                "years_code_pro not null, not special literals, contains "
                f"non-numeric characters (survey_year={year})"
            ),
            "blocks_when": lambda n: n > 0,
        },
        {
            "name": "total_rows_loaded",
            "issue_type": "AUDIT",
            "sql": (
                "SELECT COUNT(*) FROM raw.survey_responses "
                f"WHERE survey_year = {year}"
            ),
            "details": f"Total rows in raw.survey_responses for survey_year={year}",
            "blocks_when": lambda n: n == 0,
        },
        {
            # 1 = this year's raw count is under half of the last published
            # total_rows_loaded *for the same year*. 0 = no baseline for
            # this year, or the drop is within the documented 50% band.
            # See data_quality_policy.md (row_count_drop).
            "name": "row_count_drop",
            "issue_type": "VOLUME_DROP",
            "sql": f"""
                WITH cur AS (
                    SELECT COUNT(*)::numeric AS n
                    FROM raw.survey_responses
                    WHERE survey_year = {year}
                ),
                pub AS (
                    SELECT NULLIF(r.dq_summary->>'total_rows_loaded', '')::numeric AS n
                    FROM dwh.active_release a
                    JOIN dwh.pipeline_releases r ON r.release_id = a.release_id
                    WHERE a.survey_year = {year}
                )
                SELECT CASE
                    WHEN NOT EXISTS (SELECT 1 FROM pub) THEN 0
                    WHEN (SELECT n FROM pub) IS NULL THEN 0
                    WHEN (SELECT n FROM pub) <= 0 THEN 0
                    WHEN (SELECT n FROM cur) * 2 < (SELECT n FROM pub) THEN 1
                    ELSE 0
                END
            """,
            "details": (
                f"Current raw row count for survey_year={year} is under 50% "
                "of that year's last published total_rows_loaded "
                "(truncated / partial download)"
            ),
            "blocks_when": lambda n: n > 0,
        },
    ]


# Back-compat name for anything that imported DQ_CHECKS as a list. Built for
# the default year; run_checks() always rebuilds with the year it is given.
DQ_CHECKS = _dq_checks(2024)


class PublicationBlocked(Exception):
    """A DQ check that the policy says must not reach publish_release."""


def run_checks(survey_year=None) -> dict:
    """Wipe last run's dq_issues, run every check, insert one row per check.

    Returns {check_name: row_count} for pipeline_releases.dq_summary.

    We delete previous issues first so the table is "this run only." AUDIT
    is always info because the count is the table size, not a defect tally.

    After logging, any blocking check raises PublicationBlocked. The release
    row must be marked failed by the caller / on_failure_callback.

    Known year-specific absences (2023: ai_threat, job_sat) are not a
    check. There is no generic "column is all NULL" rule, and adding one
    would have to consult KNOWN_ABSENT_COLUMNS rather than fail 2023.
    """
    year = resolve_survey_year(survey_year)
    conn = get_connection()
    summary = {}
    blockers = []
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dwh.dq_issues")
            conn.commit()
            logger.info("Cleared previous run from dwh.dq_issues")

        for check in _dq_checks(year):
            name = check["name"]
            issue_type = check["issue_type"]
            sql = check["sql"].strip()
            details = check["details"]

            with conn.cursor() as cur:
                cur.execute(sql)
                row_count = cur.fetchone()[0]

            summary[name] = row_count

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dwh.dq_issues (check_name, issue_type, row_count, details)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (name, issue_type, row_count, details),
                )
            conn.commit()

            if check["blocks_when"](row_count):
                blockers.append(f"{name}={row_count}")
                logger.warning("BLOCK %s: %s (count=%d)", name, issue_type, row_count)
            elif row_count > 0 and issue_type != "AUDIT":
                logger.warning("⚠️ %s: %s (count=%d)", name, issue_type, row_count)
            else:
                logger.info("✓ %s: %s (count=%d)", name, issue_type, row_count)

        logger.info("Data quality checks completed (survey_year=%s)", year)
        if blockers:
            raise PublicationBlocked(
                "DQ policy blocked publication: " + ", ".join(blockers)
            )
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    run_checks()
