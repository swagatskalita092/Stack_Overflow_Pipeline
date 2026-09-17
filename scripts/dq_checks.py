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
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Each dict is one check. `sql` must return a single integer (a COUNT).
# `issue_type` AUDIT is "how many rows did we load?", not "how many are bad."
# `blocks_when` is a callable(row_count) -> bool from the Phase A policy.
DQ_CHECKS = [
    {
        "name": "null_response_id",
        "issue_type": "NULL_PRIMARY_KEY",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE response_id IS NULL",
        "details": "Rows with null response_id",
        "blocks_when": lambda n: n > 0,
    },
    {
        "name": "duplicate_response_id",
        "issue_type": "DUPLICATE_KEY",
        "sql": """
            SELECT COUNT(*) FROM (
                SELECT response_id FROM raw.survey_responses
                GROUP BY response_id HAVING COUNT(*) > 1
            ) dup
        """,
        "details": "Distinct response_ids appearing more than once",
        "blocks_when": lambda n: False,
    },
    {
        "name": "null_country",
        "issue_type": "MISSING_DIMENSION",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE country IS NULL",
        "details": "Rows with null country",
        "blocks_when": lambda n: False,
    },
    {
        "name": "null_comp_total",
        "issue_type": "MISSING_METRIC",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE comp_total IS NULL",
        "details": "Rows with null comp_total",
        "blocks_when": lambda n: False,
    },
    {
        "name": "invalid_years_code_pro",
        "issue_type": "FORMAT_INCONSISTENCY",
        "sql": """
            SELECT COUNT(*) FROM raw.survey_responses
            WHERE years_code_pro IS NOT NULL
              AND years_code_pro NOT IN ('Less than 1 year', 'More than 50 years')
              AND years_code_pro ~ '[^0-9]'
        """,
        "details": "years_code_pro not null, not special literals, contains non-numeric characters",
        "blocks_when": lambda n: n > 0,
    },
    {
        "name": "total_rows_loaded",
        "issue_type": "AUDIT",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses",
        "details": "Total rows in raw.survey_responses",
        "blocks_when": lambda n: n == 0,
    },
    {
        # 1 = current raw count is under half of the last published
        # total_rows_loaded. 0 = no baseline, or the drop is within the
        # documented 50% band. See data_quality_policy.md (row_count_drop).
        "name": "row_count_drop",
        "issue_type": "VOLUME_DROP",
        "sql": """
            WITH cur AS (
                SELECT COUNT(*)::numeric AS n FROM raw.survey_responses
            ),
            pub AS (
                SELECT NULLIF(r.dq_summary->>'total_rows_loaded', '')::numeric AS n
                FROM dwh.active_release a
                JOIN dwh.pipeline_releases r ON r.release_id = a.release_id
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
            "Current raw row count is under 50% of the last published "
            "total_rows_loaded (truncated / partial download)"
        ),
        "blocks_when": lambda n: n > 0,
    },
]


class PublicationBlocked(Exception):
    """A DQ check that the policy says must not reach publish_release."""


def run_checks() -> dict:
    """Wipe last run's dq_issues, run every check, insert one row per check.

    Returns {check_name: row_count} for pipeline_releases.dq_summary.

    We delete previous issues first so the table is "this run only." AUDIT
    is always info because the count is the table size, not a defect tally.

    After logging, any blocking check raises PublicationBlocked. The release
    row must be marked failed by the caller / on_failure_callback.
    """
    conn = get_connection()
    summary = {}
    blockers = []
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dwh.dq_issues")
            conn.commit()
            logger.info("Cleared previous run from dwh.dq_issues")

        for check in DQ_CHECKS:
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

        logger.info("Data quality checks completed")
        if blockers:
            raise PublicationBlocked(
                "DQ policy blocked publication: " + ", ".join(blockers)
            )
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    run_checks()
