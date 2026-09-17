"""Regression: a truncated landing table must not publish.

Phase D injection 1 published a junk ZIP because total_rows_loaded only
blocked n == 0. row_count_drop compares this load to the last published
total_rows_loaded and blocks a >50% collapse.
"""

from __future__ import annotations

from conftest import active_id, insert_expected_marts
from dq_checks import PublicationBlocked, run_checks
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
    """No prior published baseline: 13 fixture rows must still be legal."""
    summary = run_checks()
    assert summary["total_rows_loaded"] == 13
    assert summary["row_count_drop"] == 0


def test_truncated_fixture_is_blocked_instead_of_published(warehouse):
    """Publish 13 rows, then keep 2. DQ must block; the live pointer stays."""
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
