"""
Data quality checks for raw.survey_responses. Runs 6 checks and logs results to dwh.dq_issues.
"""

import logging
import os

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DQ_CHECKS = [
    {
        "name": "null_response_id",
        "issue_type": "NULL_PRIMARY_KEY",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE response_id IS NULL",
        "details": "Rows with null response_id",
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
    },
    {
        "name": "null_country",
        "issue_type": "MISSING_DIMENSION",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE country IS NULL",
        "details": "Rows with null country",
    },
    {
        "name": "null_comp_total",
        "issue_type": "MISSING_METRIC",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses WHERE comp_total IS NULL",
        "details": "Rows with null comp_total",
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
    },
    {
        "name": "total_rows_loaded",
        "issue_type": "AUDIT",
        "sql": "SELECT COUNT(*) FROM raw.survey_responses",
        "details": "Total rows in raw.survey_responses",
    },
]


def _get_db_connection():
    """Build connection from env with same defaults as ingest_survey.py."""
    host = os.getenv("SURVEY_DB_HOST", "postgres")
    port = int(os.getenv("SURVEY_DB_PORT", "5432"))
    dbname = os.getenv("SURVEY_DB_NAME", "survey_db")
    user = os.getenv("SURVEY_DB_USER", "airflow")
    password = os.getenv("SURVEY_DB_PASSWORD", "airflow")
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
    )


def run_checks() -> None:
    """Clear dwh.dq_issues, run all checks, insert results, and log with ⚠️ or ✓."""
    conn = _get_db_connection()
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

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dwh.dq_issues (check_name, issue_type, row_count, details)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (name, issue_type, row_count, details),
                )
            conn.commit()

            if row_count > 0 and issue_type != "AUDIT":
                logger.warning("⚠️ %s: %s (count=%d)", name, issue_type, row_count)
            else:
                logger.info("✓ %s: %s (count=%d)", name, issue_type, row_count)

        logger.info("Data quality checks completed")
    finally:
        conn.close()


if __name__ == "__main__":
    run_checks()
