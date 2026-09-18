"""Regression: a truncated landing table must not publish.

Phase D injection 1 published a junk ZIP because total_rows_loaded only
blocked n == 0. row_count_drop compares this load to the last published
total_rows_loaded and blocks a >50% collapse.
"""

from __future__ import annotations

from conftest import FIXTURES, active_id, insert_expected_marts
from dq_checks import PublicationBlocked, run_checks
from ingest_survey import run as ingest_run
from release import (
    ReleaseError,
    attach_dq_summary,
    mark_candidate,
    mark_failed,
    open_release,
    publish_release,
    record_source_checksum,
)


def test_first_publish_of_fixture_size_is_not_blocked(warehouse):
    """No prior published baseline: 14 fixture rows must still be legal."""
    summary = run_checks()
    assert summary["total_rows_loaded"] == 14
    assert summary["row_count_drop"] == 0


def test_truncated_fixture_is_blocked_instead_of_published(warehouse):
    """Publish 14 rows, then keep 2. DQ must block; the live pointer stays."""
    conn = warehouse
    good = open_release()
    record_source_checksum(good)
    summary = run_checks()
    attach_dq_summary(good, summary)
    insert_expected_marts(conn, good)
    mark_candidate(good)
    assert publish_release(good) == "published"
    live = active_id(conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM raw.survey_responses
            WHERE response_id NOT IN ('R001', 'R002')
            """
        )
        cur.execute("SELECT COUNT(*) FROM raw.survey_responses")
        leftover = cur.fetchone()[0]
    conn.commit()
    assert leftover == 2

    bad = open_release()
    record_source_checksum(bad)
    try:
        run_checks()
        blocked = False
        block_msg = ""
    except PublicationBlocked as exc:
        blocked = True
        block_msg = str(exc)
    assert blocked, "truncated 2-row load must raise PublicationBlocked"
    assert "row_count_drop" in block_msg
    mark_failed(bad, notes=block_msg)

    try:
        publish_release(bad)
        published = True
    except ReleaseError:
        published = False
    assert not published
    assert active_id(conn) == live


def test_first_2023_publish_skips_drop_then_second_2023_drop_blocks(warehouse):
    """First publish of a year has no baseline; a later collapse of *that* year blocks.

    row_count_drop is scoped to survey_year. A first 2023 load must not be
    compared to 2024's published total_rows_loaded (here faked at 65437 so
    a global compare would definitely fire). After 2023 is published with
    9 rows, cutting that year to 2 must block, and 2024's pointer stays.
    """
    conn = warehouse
    y24 = open_release(survey_year=2024)
    record_source_checksum(y24)
    attach_dq_summary(y24, {"total_rows_loaded": 65437})
    insert_expected_marts(conn, y24, survey_year=2024)
    mark_candidate(y24)
    assert publish_release(y24) == "published"
    live_2024 = active_id(conn, 2024)

    ingest_run(
        survey_year=2023,
        source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
    )
    first_2023 = open_release(survey_year=2023)
    record_source_checksum(first_2023)
    summary = run_checks(survey_year=2023)
    assert summary["total_rows_loaded"] == 9
    assert summary["row_count_drop"] == 0, (
        "first 2023 publish must not treat 2024's 65437 as its baseline"
    )
    attach_dq_summary(first_2023, summary)
    insert_expected_marts(
        conn,
        first_2023,
        survey_year=2023,
        salary_path=FIXTURES / "expected_mart_salary_analytics_2023.csv",
        ai_path=FIXTURES / "expected_mart_ai_sentiment_2023.csv",
    )
    mark_candidate(first_2023)
    assert publish_release(first_2023) == "published"
    live_2023 = active_id(conn, 2023)

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM raw.survey_responses
            WHERE survey_year = 2023
              AND response_id NOT IN ('G001', 'G002')
            """
        )
        cur.execute(
            "SELECT COUNT(*) FROM raw.survey_responses WHERE survey_year = 2023"
        )
        leftover = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM raw.survey_responses WHERE survey_year = 2024"
        )
        still_2024 = cur.fetchone()[0]
    conn.commit()
    assert leftover == 2
    assert still_2024 == 14

    second_2023 = open_release(survey_year=2023)
    record_source_checksum(second_2023)
    try:
        run_checks(survey_year=2023)
        blocked = False
        block_msg = ""
    except PublicationBlocked as exc:
        blocked = True
        block_msg = str(exc)
    assert blocked, "second 2023 run with 9 → 2 must raise PublicationBlocked"
    assert "row_count_drop" in block_msg
    mark_failed(second_2023, notes=block_msg)

    try:
        publish_release(second_2023)
        published = True
    except ReleaseError:
        published = False
    assert not published
    assert active_id(conn, 2023) == live_2023
    assert active_id(conn, 2024) == live_2024

