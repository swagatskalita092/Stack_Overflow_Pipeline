"""Phase E: 2023 schema drift and two-year publication coexistence.

The 2023 public extract is missing AIThreat and JobSat. Ingest must warn,
load NULLs, and not treat that as a DQ failure. Publishing a 2023 release
must not move, hide, or corrupt the currently published 2024 release.
"""

from __future__ import annotations

import logging

from conftest import (
    FIXTURES,
    active_id,
    build_good_release,
    view_salary_count,
    view_salary_release_ids,
)
from dq_checks import PublicationBlocked, known_absent_columns, run_checks
from ingest_survey import run as ingest_run
from release import mark_candidate, open_release, publish_release, record_source_checksum
from test_mart_correctness import (
    AI_KEYS,
    AI_NUMS,
    SALARY_KEYS,
    SALARY_NUMS,
    _compare_mart,
    _csv_rows,
    _run_dbt,
    _view_rows,
)
from test_mart_correctness import (
    AI_KEYS,
    AI_NUMS,
    SALARY_KEYS,
    SALARY_NUMS,
    _compare_mart,
    _csv_rows,
    _run_dbt,
    _view_rows,
)


def test_ingest_2023_fixture_warns_and_nulls_missing_columns(warehouse, caplog):
    """Real 2023 shape: AIThreat/JobSat absent, not present-but-empty.

    2024 fixture rows already in the warehouse must survive the 2023 load
    (year-scoped DELETE, not TRUNCATE).
    """
    conn = warehouse
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.survey_responses WHERE survey_year = 2024")
        n_2024_before = cur.fetchone()[0]
    assert n_2024_before == 13

    with caplog.at_level(logging.WARNING, logger="ingest_survey"):
        ingest_run(
            survey_year=2023,
            source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
        )
    warnings = " ".join(r.message for r in caplog.records)
    assert "AIThreat" in warnings
    assert "JobSat" in warnings

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.survey_responses WHERE survey_year = 2024")
        assert cur.fetchone()[0] == n_2024_before
        cur.execute("SELECT COUNT(*) FROM raw.survey_responses WHERE survey_year = 2023")
        n_2023 = cur.fetchone()[0]
        assert n_2023 == 9
        cur.execute(
            """
            SELECT COUNT(*) FROM raw.survey_responses
            WHERE survey_year = 2023 AND (ai_threat IS NOT NULL OR job_sat IS NOT NULL)
            """
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*) FROM raw.survey_responses
            WHERE survey_year = 2023 AND ai_threat IS NULL AND job_sat IS NULL
            """
        )
        assert cur.fetchone()[0] == 9


def test_dq_does_not_flag_known_2023_ai_threat_absence(warehouse):
    """All-NULL ai_threat/job_sat is the 2023 schema, not a quality failure.

    Current checks have no generic null-rate on those columns. This test
    locks that: run_checks(2023) must complete, and KNOWN_ABSENT_COLUMNS
    documents the allowlist a later null-rate check would have to honour.
    """
    ingest_run(
        survey_year=2023,
        source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
    )
    assert known_absent_columns(2023) == frozenset({"ai_threat", "job_sat"})
    assert known_absent_columns(2024) == frozenset()

    try:
        summary = run_checks(survey_year=2023)
        blocked = False
        block_msg = ""
    except PublicationBlocked as exc:
        blocked = True
        block_msg = str(exc)
        summary = {}
    assert not blocked, f"2023 known column absence must not block: {block_msg}"
    assert summary["total_rows_loaded"] == 9
    assert summary["row_count_drop"] == 0
    assert summary["null_response_id"] == 0

    conn = warehouse
    with conn.cursor() as cur:
        cur.execute("SELECT check_name FROM dwh.dq_issues")
        names = {r[0] for r in cur.fetchall()}
    assert "null_ai_threat" not in names
    assert "null_job_sat" not in names


def test_publishing_2023_does_not_move_2024_active(warehouse):
    """Direct two-year analog of test_older_candidate_cannot_roll_back_newer_publish.

    seq is a global BIGSERIAL. A 2023 release opened after 2024 publishes
    will have a higher seq. Publishing 2023 must still leave 2024's pointer
    and 2024's view rows alone, and 2023 views must show the 2023 marts.
    """
    conn = warehouse
    newer_2024 = build_good_release(conn, survey_year=2024)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seq FROM dwh.pipeline_releases WHERE release_id = %s::uuid",
            (newer_2024,),
        )
        seq_2024 = cur.fetchone()[0]
    assert publish_release(newer_2024) == "published"
    assert active_id(conn, 2024) == newer_2024
    assert view_salary_release_ids(conn) == {newer_2024}
    assert view_salary_count(conn) == 1

    ingest_run(
        survey_year=2023,
        source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
    )
    older_year = build_good_release(
        conn,
        survey_year=2023,
        salary_path=FIXTURES / "expected_mart_salary_analytics_2023.csv",
        ai_path=FIXTURES / "expected_mart_ai_sentiment_2023.csv",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seq FROM dwh.pipeline_releases WHERE release_id = %s::uuid",
            (older_year,),
        )
        seq_2023 = cur.fetchone()[0]
    assert seq_2023 > seq_2024, (
        "this test is only meaningful if the 2023 candidate's global seq "
        f"is higher than 2024's ({seq_2023} vs {seq_2024})"
    )

    assert publish_release(older_year) == "published"
    assert active_id(conn, 2024) == newer_2024
    assert active_id(conn, 2023) == older_year

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT survey_year, country, respondent_count, max_salary, release_id::text
            FROM marts.v_salary_analytics
            ORDER BY survey_year
            """
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    year_2023, country_2023, n_2023, max_2023, rid_2023 = rows[0]
    year_2024, country_2024, n_2024, max_2024, rid_2024 = rows[1]
    assert year_2023 == 2023
    assert country_2023 == "Germany"
    assert n_2023 == 6
    assert float(max_2023) == 130000.0
    assert rid_2023 == older_year
    assert year_2024 == 2024
    assert country_2024 == "United States"
    assert n_2024 == 6
    assert float(max_2024) == 150000.0
    assert rid_2024 == newer_2024

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT survey_year, country, pct_see_ai_as_threat, avg_job_satisfaction
            FROM marts.v_ai_sentiment
            WHERE survey_year = 2023
            ORDER BY country
            """
        )
        ai_2023 = cur.fetchall()
    assert len(ai_2023) == 2
    for _year, _country, pct, job in ai_2023:
        assert pct is None
        assert job is None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT country, pct_see_ai_as_threat
            FROM marts.v_ai_sentiment
            WHERE survey_year = 2024
            """
        )
        ai_2024 = cur.fetchall()
    assert ai_2024
    assert all(row[1] is not None for row in ai_2024)

    # A later 2023 publish still must not roll 2024 back or hide it.
    second_2023 = build_good_release(
        conn,
        survey_year=2023,
        salary_path=FIXTURES / "expected_mart_salary_analytics_2023.csv",
        ai_path=FIXTURES / "expected_mart_ai_sentiment_2023.csv",
    )
    assert publish_release(second_2023) == "published"
    assert active_id(conn, 2024) == newer_2024
    assert active_id(conn, 2023) == second_2023
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT survey_year, release_id::text FROM marts.v_salary_analytics
            ORDER BY survey_year
            """
        )
        after = cur.fetchall()
    assert after == [(2023, second_2023), (2024, newer_2024)]


PHASE_E_2023_RELEASE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_dbt_2023_ai_threat_rate_is_null_not_zero(warehouse):
    """Real dbt: 2023 cells must not compute pct_see_ai_as_threat from all-NULL.

    Decision: keep 2023 in mart_ai_sentiment (AISelect/AISent exist) but set
    pct_see_ai_as_threat to NULL when COUNT(ai_threat) = 0. 0.0 would mean
    "nobody in this cell sees AI as a threat," which is a lie when the
    question was not on the survey. avg_job_satisfaction is NULL for the
    same reason (JobSat absent).
    """
    conn = warehouse
    ingest_run(
        survey_year=2023,
        source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
    )
    rid = PHASE_E_2023_RELEASE_ID
    open_release(release_id=rid, survey_year=2023)
    record_source_checksum(rid)
    _run_dbt("run", rid, survey_year=2023)
    _run_dbt("test", rid, survey_year=2023)
    mark_candidate(rid)
    assert publish_release(rid) == "published"

    problems: list[str] = []
    problems += _compare_mart(
        name="v_salary_analytics_2023",
        actual=_view_rows(
            conn,
            "SELECT * FROM marts.v_salary_analytics WHERE survey_year = 2023",
        ),
        expected=_csv_rows(FIXTURES / "expected_mart_salary_analytics_2023.csv"),
        keys=SALARY_KEYS,
        numeric=SALARY_NUMS,
    )
    problems += _compare_mart(
        name="v_ai_sentiment_2023",
        actual=_view_rows(
            conn,
            "SELECT * FROM marts.v_ai_sentiment WHERE survey_year = 2023",
        ),
        expected=_csv_rows(FIXTURES / "expected_mart_ai_sentiment_2023.csv"),
        keys=AI_KEYS,
        numeric=AI_NUMS,
    )
    if problems:
        raise AssertionError(
            "2023 dbt output did not match the hand-calculated 2023 fixtures "
            "(pct_see_ai_as_threat must be NULL, not 0.0):\n- "
            + "\n- ".join(problems)
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM marts.v_ai_sentiment
            WHERE survey_year = 2023 AND pct_see_ai_as_threat IS NOT NULL
            """
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*) FROM marts.v_ai_sentiment
            WHERE survey_year = 2023 AND avg_job_satisfaction IS NOT NULL
            """
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*) FROM marts.v_ai_sentiment WHERE survey_year = 2023
            """
        )
        assert cur.fetchone()[0] == 2

