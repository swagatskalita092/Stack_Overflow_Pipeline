"""Publication-safety cases (the warehouse equivalent of chaos tests).

Readers always go through marts.v_*. A passing test means a bad or unfinished
run did not become the official answer.
"""

from __future__ import annotations

import threading
import time

from conftest import (
    ROOT,
    active_id,
    build_good_release,
    dbt_test_not_null_respondent_count,
    insert_expected_marts,
    release_status,
    table_salary_count,
    view_salary_count,
    view_salary_release_ids,
)
from release import (
    ReleaseError,
    mark_failed,
    open_release,
    publish_release,
    record_source_checksum,
)


def test_identical_rerun_views_show_one_release_not_doubled(warehouse):
    """Two builds of the same fixtures: table has 2x rows, views have 1x."""
    conn = warehouse
    first = build_good_release(conn)
    assert publish_release(first) == "published"
    one_view = view_salary_count(conn)
    assert one_view == 1
    assert view_salary_release_ids(conn) == {first}
    assert table_salary_count(conn) == 1

    second = build_good_release(conn)
    assert release_status(conn, second) == "candidate_ready_unchanged_source"
    assert table_salary_count(conn) == 2
    # Not published yet: views still the first release, not a mix.
    assert view_salary_count(conn) == 1
    assert view_salary_release_ids(conn) == {first}

    assert publish_release(second) == "published"
    assert view_salary_count(conn) == 1
    assert view_salary_release_ids(conn) == {second}
    assert table_salary_count(conn) == 2
    assert active_id(conn) == second


def test_failed_dbt_test_leaves_previous_release_in_views(warehouse):
    """Second run fails the not_null check; views stay on the good release."""
    conn = warehouse
    good = build_good_release(conn)
    publish_release(good)

    bad = open_release()
    record_source_checksum(bad)
    insert_expected_marts(conn, bad, corrupt_salary=True)
    assert dbt_test_not_null_respondent_count(conn, bad) is False
    mark_failed(bad, notes="simulated dbt_test_models not_null respondent_count")

    assert release_status(conn, bad) == "failed"
    try:
        publish_release(bad)
        raised = False
    except ReleaseError:
        raised = True
    assert raised, "failed releases must not be publishable"
    assert active_id(conn) == good
    assert view_salary_release_ids(conn) == {good}
    assert view_salary_count(conn) == 1


def test_crash_between_candidate_and_publish_then_retry(warehouse):
    """Crash after candidate_ready: views stay old; retry publishes exactly once."""
    conn = warehouse
    old = build_good_release(conn)
    publish_release(old)

    new = build_good_release(conn)
    assert release_status(conn, new) in {
        "candidate_ready",
        "candidate_ready_unchanged_source",
    }
    # Crash: process dies before publish_release. Pointer must not move.
    assert active_id(conn) == old
    assert view_salary_release_ids(conn) == {old}

    first_try = publish_release(new)
    assert first_try == "published"
    assert active_id(conn) == new
    assert view_salary_release_ids(conn) == {new}

    retry = publish_release(new)
    assert retry == "already_active"
    assert active_id(conn) == new
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM dwh.active_release")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT COUNT(*) FROM dwh.pipeline_releases WHERE status = 'published'"
        )
        # old + new; retry must not insert a second active row
        assert cur.fetchone()[0] == 2


def test_overlapping_publishes_serialize_and_leave_one_pointer(warehouse):
    """Two threads publish the same candidate: one published, one already_active.

    The advisory lock serializes them. Exactly one active_release row. Views
    show that one id. Two *different* candidates racing is covered by
    test_older_candidate_cannot_roll_back_newer_publish (monotonic seq).
    """
    conn = warehouse
    rid = build_good_release(conn)

    hold = threading.Event()
    started = threading.Event()
    results = []
    errors = {}

    def slow_publish():
        def after_lock():
            started.set()
            hold.wait(timeout=5)

        try:
            results.append(publish_release(rid, after_lock=after_lock))
        except Exception as exc:
            errors["slow"] = exc

    def waiting_publish():
        started.wait(timeout=5)
        try:
            results.append(publish_release(rid))
        except Exception as exc:
            errors["wait"] = exc

    t1 = threading.Thread(target=slow_publish)
    t2 = threading.Thread(target=waiting_publish)
    t1.start()
    t2.start()
    time.sleep(0.2)
    hold.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"publish raised: {errors}"
    assert sorted(results) == ["already_active", "published"]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM dwh.active_release")
        assert cur.fetchone()[0] == 1
    assert active_id(conn) == rid
    assert view_salary_release_ids(conn) == {rid}
    assert view_salary_count(conn) == 1


def test_older_candidate_cannot_roll_back_newer_publish(warehouse):
    """Slower older run must not overwrite a newer published release.

    Built out of order on purpose: open older, open newer (higher seq),
    publish newer first, then older tries to publish. Older is superseded;
    views stay on newer.
    """
    conn = warehouse
    older = build_good_release(conn)
    newer = build_good_release(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT release_id::text, seq
            FROM dwh.pipeline_releases
            WHERE release_id IN (%s::uuid, %s::uuid)
            ORDER BY seq
            """,
            (older, newer),
        )
        rows = cur.fetchall()
    assert rows[0][0] == older
    assert rows[1][0] == newer
    assert rows[0][1] < rows[1][1]

    assert publish_release(newer) == "published"
    assert active_id(conn) == newer
    assert view_salary_release_ids(conn) == {newer}

    assert publish_release(older) == "superseded"
    assert release_status(conn, older) == "superseded"
    assert active_id(conn) == newer
    assert view_salary_release_ids(conn) == {newer}
    assert view_salary_count(conn) == 1
    assert publish_release(older) == "superseded"


def test_dag_wires_publish_after_dbt_test():
    """Static check: Airflow order is dbt_test → mark_candidate → publish.

    Does not run the scheduler. Catches a regression where publish is wired
    before tests, which would re-create the original hazard.
    """
    dag = (ROOT / "airflow" / "dags" / "stackoverflow_pipeline_dag.py").read_text(
        encoding="utf-8"
    )
    assert 'task_id="publish_release"' in dag
    assert 'task_id="mark_candidate"' in dag
    assert "dbt_test_models" in dag
    # Downstream chain at the bottom of the file.
    chain_index = dag.rfind("dbt_test_models")
    mark_index = dag.rfind("mark_candidate")
    pub_index = dag.rfind("publish_release")
    assert chain_index < mark_index < pub_index
