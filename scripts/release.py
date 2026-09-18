"""Append-only release pointer: build many, publish one per year, readers see both.

Why this module exists
----------------------
dbt used to CREATE TABLE the marts on every run. A crash mid-run left
analysts looking at a half-built table. FlashBuy's chaos tests ask "what
does the buyer see if we die between steps?" — this is that question for
warehouse publication.

Each pipeline run owns a release_id and a survey_year. Mart rows for that
id are INSERTed next to older ids. dwh.active_release holds one id *per
year* that marts.v_* views join to on (survey_year, release_id).
publish_release() moves that year's pointer in one transaction under an
advisory lock, so two overlapping publishes cannot interleave updates
and leave a mix.

A bad run stays in pipeline_releases as failed, candidate_ready, or
superseded and never becomes the view output unless someone publishes a
*newer* (higher seq) candidate *for that same year*. An older run that
finishes late cannot roll the pointer backward. seq is still a global
BIGSERIAL; the comparison is scoped to the candidate's survey_year so a
2023 release built after several 2024 releases is not "newer than 2024"
and cannot move 2024's pointer.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection
from ingest_survey import resolve_survey_year

logger = logging.getLogger(__name__)

# Arbitrary stable key so every publish serializes on the same Postgres lock.
# pg_advisory_xact_lock lives for the transaction; it dies on commit/rollback.
PUBLISH_LOCK_KEY = 87420133

CANDIDATE_STATUSES = ("candidate_ready", "candidate_ready_unchanged_source")


class ReleaseError(Exception):
    """Something about this release makes publish unsafe."""


def get_git_sha() -> Optional[str]:
    """HEAD of the checkout, or None if git is missing / not a repo.

    Stored for forensics ("which code built this candidate?"). It is not
    used as the uniqueness key — two runs of the same SHA still get two
    release_ids, because source data can change without a code change.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha or None
    except (OSError, subprocess.CalledProcessError):
        return None


def compute_source_checksum(cur, survey_year: int) -> str:
    """Stable md5 of every raw.survey_responses row for this survey_year.

    CAST(t.* AS text) is Postgres's whole-row text. We order by that hash
    so insert order does not change the checksum. Empty table hashes as
    md5('') so a zero-row ingest is distinguishable from "we forgot to
    checksum." Scoped per year so loading 2023 does not change 2024's
    "unchanged source" comparison.
    """
    cur.execute(
        """
        SELECT COALESCE(md5(string_agg(row_md5, '' ORDER BY row_md5)), md5(''))
        FROM (
            SELECT md5(CAST(t.* AS text)) AS row_md5
            FROM raw.survey_responses t
            WHERE t.survey_year = %s
        ) s
        """,
        (int(survey_year),),
    )
    return cur.fetchone()[0]


def _release_survey_year(cur, release_id: str) -> int:
    """survey_year stamped when this release was opened."""
    cur.execute(
        """
        SELECT survey_year
        FROM dwh.pipeline_releases
        WHERE release_id = %s::uuid
        """,
        (release_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ReleaseError(f"unknown release_id {release_id}")
    return int(row[0])


def open_release(
    release_id: Optional[str] = None,
    git_sha: Optional[str] = None,
    survey_year=None,
) -> str:
    """Insert a building row and return the new release_id (uuid str).

    Call this at the start of a run, before dbt, so a later ingest/DQ/dbt
    failure still has a row to mark failed. Checksum is filled in after
    ingest by record_source_checksum().

    If release_id is passed (Airflow maps dag run_id → uuid5), a retry of
    this task does not mint a second row.
    """
    if release_id is None:
        release_id = str(uuid.uuid4())
    if git_sha is None:
        git_sha = get_git_sha()
    year = resolve_survey_year(survey_year)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dwh.pipeline_releases
                    (release_id, git_sha, status, survey_year)
                VALUES (%s::uuid, %s, 'building', %s)
                ON CONFLICT (release_id) DO NOTHING
                """,
                (release_id, git_sha, year),
            )
        conn.commit()
        logger.info("Opened release %s (building, survey_year=%s)", release_id, year)
        return release_id
    finally:
        conn.close()


def record_source_checksum(release_id: str) -> str:
    """Hash this year's raw.survey_responses and flag identical-to-published source.

    We still build and (if tests pass) still publish. The flag only changes
    the candidate status name so operators can see "this was a no-op ingest"
    without us silently skipping the run. Comparison is against this year's
    active release, not "whatever row happens to come back from active_release."
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            year = _release_survey_year(cur, release_id)
            checksum = compute_source_checksum(cur, year)
            cur.execute(
                """
                SELECT r.source_checksum
                FROM dwh.active_release a
                JOIN dwh.pipeline_releases r ON r.release_id = a.release_id
                WHERE a.survey_year = %s
                """,
                (year,),
            )
            row = cur.fetchone()
            published_checksum = row[0] if row else None
            unchanged = bool(
                published_checksum is not None and published_checksum == checksum
            )
            cur.execute(
                """
                UPDATE dwh.pipeline_releases
                SET source_checksum = %s,
                    unchanged_source = %s
                WHERE release_id = %s::uuid
                """,
                (checksum, unchanged, release_id),
            )
        conn.commit()
        logger.info(
            "Release %s checksum=%s unchanged_source=%s survey_year=%s",
            release_id,
            checksum,
            unchanged,
            year,
        )
        return checksum
    finally:
        conn.close()


def attach_dq_summary(release_id: str, summary: dict[str, Any]) -> None:
    """Store the DQ counts on the release row (JSONB)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dwh.pipeline_releases
                SET dq_summary = %s::jsonb
                WHERE release_id = %s::uuid
                """,
                (json.dumps(summary), release_id),
            )
        conn.commit()
    finally:
        conn.close()


def mark_failed(release_id: Optional[str], notes: str = "") -> None:
    """Terminal status: this id must never be published.

    Safe to call from Airflow on_failure_callback. No-op if we never opened
    a release (xcom empty) so a failure before open_release does not crash
    the callback.
    """
    if not release_id:
        logger.warning("mark_failed called with no release_id; nothing to record")
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dwh.pipeline_releases
                SET status = 'failed',
                    notes = %s
                WHERE release_id = %s::uuid
                  AND status <> 'published'
                """,
                (notes[:2000], release_id),
            )
        conn.commit()
        logger.info("Release %s marked failed: %s", release_id, notes)
    finally:
        conn.close()


def mark_candidate(release_id: str) -> str:
    """dbt tests passed. Status becomes candidate_ready(_unchanged_source).

    Does not move dwh.active_release. A crash after this function and before
    publish_release must leave readers on the previous published id.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, unchanged_source
                FROM dwh.pipeline_releases
                WHERE release_id = %s::uuid
                """,
                (release_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ReleaseError(f"unknown release_id {release_id}")
            status, unchanged = row
            if status in ("failed", "superseded"):
                raise ReleaseError(
                    f"refusing to candidate a {status} release {release_id}"
                )
            if status == "published":
                return status
            new_status = (
                "candidate_ready_unchanged_source" if unchanged else "candidate_ready"
            )
            cur.execute(
                """
                UPDATE dwh.pipeline_releases
                SET status = %s
                WHERE release_id = %s::uuid
                """,
                (new_status, release_id),
            )
        conn.commit()
        logger.info("Release %s is %s", release_id, new_status)
        return new_status
    finally:
        conn.close()


def _xcom_release_id(context: dict) -> Optional[str]:
    """Read the id open_release pushed. Used by Airflow callbacks."""
    ti = context.get("ti")
    if ti is None:
        return None
    return ti.xcom_pull(task_ids="open_release")


def on_release_failed(context: dict) -> None:
    """Airflow on_failure_callback: stamp the open release as failed."""
    rid = _xcom_release_id(context)
    err = context.get("exception")
    mark_failed(rid, notes=str(err) if err else "task failed")


def publish_release(
    release_id: str,
    after_lock: Optional[Callable[[], None]] = None,
) -> str:
    """Point this year's dwh.active_release row at this candidate, locked.

    after_lock is a test hook (widen a race window). Production DAG leaves
    it None.

    Returns a short result tag: 'published', 'already_active', 'superseded'.
    Raises ReleaseError if the row is not a candidate (still building, or
    failed). Retrying after a crash is already_active when this year's
    pointer already matches.

    Monotonicity is *per survey_year*. dwh.pipeline_releases.seq is a
    global BIGSERIAL, so a 2023 release opened after several 2024 releases
    has a numerically higher seq. Comparing that seq to "the" active
    release globally would either (a) treat 2023 as newer than 2024 and
    move the wrong pointer, or (b) treat a stale 2024 rerun as older than
    a later 2023 seq and wrongly supersede it. We read and upsert only
    WHERE survey_year = the candidate's year.
    """
    conn = get_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (PUBLISH_LOCK_KEY,))
            if after_lock is not None:
                after_lock()

            cur.execute(
                """
                SELECT status, seq, survey_year
                FROM dwh.pipeline_releases
                WHERE release_id = %s::uuid
                FOR UPDATE
                """,
                (release_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ReleaseError(f"unknown release_id {release_id}")
            status, candidate_seq, survey_year = row
            survey_year = int(survey_year)

            cur.execute(
                """
                SELECT a.release_id
                FROM dwh.active_release a
                WHERE a.survey_year = %s
                FOR UPDATE
                """,
                (survey_year,),
            )
            active = cur.fetchone()
            active_id = str(active[0]) if active else None
            active_seq = None
            if active_id is not None:
                cur.execute(
                    """
                    SELECT seq FROM dwh.pipeline_releases
                    WHERE release_id = %s::uuid
                    """,
                    (active_id,),
                )
                seq_row = cur.fetchone()
                active_seq = seq_row[0] if seq_row else None

            if active_id == release_id:
                if status != "published":
                    cur.execute(
                        """
                        UPDATE dwh.pipeline_releases
                        SET status = 'published',
                            published_at = COALESCE(published_at, NOW())
                        WHERE release_id = %s::uuid
                        """,
                        (release_id,),
                    )
                conn.commit()
                logger.info(
                    "Release %s already active for survey_year=%s; publish is a no-op",
                    release_id,
                    survey_year,
                )
                return "already_active"

            if (
                active_seq is not None
                and candidate_seq is not None
                and candidate_seq <= active_seq
            ):
                cur.execute(
                    """
                    UPDATE dwh.pipeline_releases
                    SET status = 'superseded',
                        notes = %s
                    WHERE release_id = %s::uuid
                      AND status <> 'published'
                    """,
                    (
                        f"seq {candidate_seq} is not newer than active seq "
                        f"{active_seq} for survey_year={survey_year}",
                        release_id,
                    ),
                )
                conn.commit()
                logger.info(
                    "Release %s superseded (seq %s <= active seq %s, survey_year=%s)",
                    release_id,
                    candidate_seq,
                    active_seq,
                    survey_year,
                )
                return "superseded"

            if status not in CANDIDATE_STATUSES and status != "published":
                raise ReleaseError(
                    f"release {release_id} status={status} is not publishable"
                )

            cur.execute(
                """
                INSERT INTO dwh.active_release (survey_year, release_id, updated_at)
                VALUES (%s, %s::uuid, NOW())
                ON CONFLICT (survey_year) DO UPDATE
                SET release_id = EXCLUDED.release_id,
                    updated_at = NOW()
                """,
                (survey_year, release_id),
            )
            cur.execute(
                """
                UPDATE dwh.pipeline_releases
                SET status = 'published',
                    published_at = NOW()
                WHERE release_id = %s::uuid
                """,
                (release_id,),
            )
        conn.commit()
        logger.info("Published release %s (survey_year=%s)", release_id, survey_year)
        return "published"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_active_release_id(survey_year=None) -> Optional[str]:
    """The id the views join to for this year, or None if that year is unpublished."""
    year = resolve_survey_year(survey_year)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT release_id FROM dwh.active_release
                WHERE survey_year = %s
                """,
                (year,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    finally:
        conn.close()

