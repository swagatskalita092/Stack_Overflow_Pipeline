"""Shared fixtures for publication-safety tests.

These tests talk to a real Postgres (the Compose `postgres` service, or any
SURVEY_DB_* target). They do not start Airflow. They call scripts/release.py
the same way the DAG's publish_release task does, and they load Phase A
fixture CSVs as the "dbt built this release" stand-in.

Why not run dbt here: the four cases are about the pointer and the views.
Simulating a successful/failed dbt_test with SQL is enough to prove that a
bad or half-finished run cannot become what a reader sees. A gap: Airflow
task wiring is checked by reading the DAG file, not by running the scheduler.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import pytest
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("SURVEY_DB_HOST", "localhost")
os.environ.setdefault("SURVEY_DB_PORT", "5432")
os.environ.setdefault("SURVEY_DB_NAME", "survey_db")
os.environ.setdefault("SURVEY_DB_USER", "airflow")
os.environ.setdefault("SURVEY_DB_PASSWORD", "airflow")

from db import get_connection  # noqa: E402
from release import (  # noqa: E402
    mark_candidate,
    mark_failed,
    open_release,
    publish_release,
    record_source_checksum,
)


def _psycopg_sql(text: str) -> str:
    """Drop psql meta-commands (\\connect) so psycopg2 can run the migrate file."""
    lines = []
    for line in text.splitlines():
        if line.startswith("\\"):
            continue
        lines.append(line)
    return "\n".join(lines)


def apply_migrate(conn) -> None:
    """Create dwh/raw/marts objects. Idempotent."""
    sql = _psycopg_sql((SCRIPTS / "migrate_release_safety.sql").read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def wipe_warehouse(conn) -> None:
    """Empty publication state between tests. Keep the schema."""
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE
                marts.mart_salary_analytics,
                marts.mart_tech_adoption,
                marts.mart_ai_sentiment,
                dwh.active_release,
                dwh.pipeline_releases,
                raw.survey_responses,
                dwh.dq_issues
            RESTART IDENTITY CASCADE
            """
        )
    conn.commit()


def load_raw_fixtures(conn) -> None:
    """Load tests/fixtures/raw_survey_responses.csv into raw.survey_responses."""
    path = FIXTURES / "raw_survey_responses.csv"
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("raw fixture is empty")
    columns = list(rows[0].keys())
    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(columns)
    insert = f"INSERT INTO raw.survey_responses ({col_sql}) VALUES ({placeholders})"
    tuples = []
    for row in rows:
        tuples.append(tuple(None if v == "" else v for v in (row[c] for c in columns)))
    with conn.cursor() as cur:
        cur.executemany(insert, tuples)
    conn.commit()


def insert_expected_marts(conn, release_id: str, *, corrupt_salary: bool = False) -> None:
    """Stand-in for a successful (or deliberately broken) dbt run.

    Copies Phase A expected mart CSVs, tagged with this release_id.
    Tech-adoption expected file is headers-only (the >=100 cutoff).
    corrupt_salary=True inserts a NULL respondent_count so a not_null test fails.
    """
    salary_path = FIXTURES / "expected_mart_salary_analytics.csv"
    with salary_path.open(encoding="utf-8", newline="") as f:
        salary_rows = list(csv.DictReader(f))
    with conn.cursor() as cur:
        for row in salary_rows:
            cur.execute(
                """
                INSERT INTO marts.mart_salary_analytics (
                    release_id, country, experience_band, dev_type, remote_work,
                    org_size, respondent_count, avg_salary, median_salary,
                    p25_salary, p75_salary, min_salary, max_salary
                ) VALUES (
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    release_id,
                    row["country"],
                    row["experience_band"],
                    row["dev_type"],
                    row["remote_work"],
                    row["org_size"],
                    None if corrupt_salary else int(row["respondent_count"]),
                    row["avg_salary"],
                    row["median_salary"],
                    row["p25_salary"],
                    row["p75_salary"],
                    row["min_salary"],
                    row["max_salary"],
                ),
            )
        ai_path = FIXTURES / "expected_mart_ai_sentiment.csv"
        with ai_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                cur.execute(
                    """
                    INSERT INTO marts.mart_ai_sentiment (
                        release_id, country, dev_type, ai_select, ai_sent, ai_threat,
                        respondent_count, avg_job_satisfaction, pct_see_ai_as_threat
                    ) VALUES (
                        %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        release_id,
                        row["country"],
                        row["dev_type"],
                        row["ai_select"],
                        row["ai_sent"],
                        row["ai_threat"],
                        int(row["respondent_count"]),
                        row["avg_job_satisfaction"],
                        row["pct_see_ai_as_threat"],
                    ),
                )
    conn.commit()


def dbt_test_not_null_respondent_count(conn, release_id: str) -> bool:
    """The mart_salary_analytics not_null test, scoped to this release.

    Returns True if the test passes. A full dbt test run is not invoked here.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM marts.mart_salary_analytics
            WHERE release_id = %s::uuid AND respondent_count IS NULL
            """,
            (release_id,),
        )
        return cur.fetchone()[0] == 0


def view_salary_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM marts.v_salary_analytics")
        return cur.fetchone()[0]


def view_salary_release_ids(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT release_id::text FROM marts.v_salary_analytics")
        return {r[0] for r in cur.fetchall()}


def table_salary_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM marts.mart_salary_analytics")
        return cur.fetchone()[0]


def active_id(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT release_id::text FROM dwh.active_release")
        row = cur.fetchone()
        return row[0] if row else None


def release_status(conn, release_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM dwh.pipeline_releases WHERE release_id = %s::uuid",
            (release_id,),
        )
        return cur.fetchone()[0]


def build_good_release(conn) -> str:
    """open → checksum → insert Phase A marts → candidate. Does not publish."""
    rid = open_release()
    record_source_checksum(rid)
    insert_expected_marts(conn, rid)
    mark_candidate(rid)
    return rid


@pytest.fixture(scope="session")
def pg_available():
    """Skip the session if Compose Postgres is not reachable."""
    try:
        conn = get_connection()
        conn.close()
        return
    except psycopg2.OperationalError as exc:
        msg = str(exc)
        if "does not exist" in msg:
            try:
                admin = psycopg2.connect(
                    host=os.environ["SURVEY_DB_HOST"],
                    port=os.environ["SURVEY_DB_PORT"],
                    dbname="airflow",
                    user=os.environ["SURVEY_DB_USER"],
                    password=os.environ["SURVEY_DB_PASSWORD"],
                )
                admin.autocommit = True
                dbname = os.environ["SURVEY_DB_NAME"]
                with admin.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
                    if cur.fetchone() is None:
                        cur.execute(f'CREATE DATABASE "{dbname}"')
                admin.close()
                return
            except Exception as inner:
                pytest.skip(
                    f"Postgres reachable but could not create {os.environ['SURVEY_DB_NAME']} ({inner})"
                )
        pytest.skip(
            f"Postgres not reachable ({exc}). Start: docker compose up -d postgres"
        )


@pytest.fixture
def warehouse(pg_available):
    """Migrated warehouse, empty between tests."""
    conn = get_connection()
    apply_migrate(conn)
    wipe_warehouse(conn)
    load_raw_fixtures(conn)
    yield conn
    wipe_warehouse(conn)
    conn.close()
