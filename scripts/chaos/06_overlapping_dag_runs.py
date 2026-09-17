"""Injection 6: two overlapping real DAG runs.

Sets max_active_runs=2, triggers two runs a few seconds apart. Both use the
local fixture ZIP so we are testing Airflow concurrency + seq monotonicity,
not the CDN. After both finish, the live pointer must be the higher seq
that actually published — never rolled back to an older seq.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "chaos"))

from _common import (  # noqa: E402
    CHAOS_DATA,
    DAG_PATH,
    INGEST_PATH,
    dag_with,
    fixture_survey_csv_text,
    ingest_from_local_zip_source,
    patched_file,
    print_report,
    snapshot,
    task_states,
    trigger,
    unpause,
    wait_dag_terminal,
    write_survey_zip,
)

SLEEP = """
            import time as _chaos_time
            logger.info("CHAOS: slow ingest so two DAG runs overlap")
            _chaos_time.sleep(20)
"""


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(False))
    before = snapshot()
    ingest_src = ingest_from_local_zip_source(zip_path, extra_load_lines=SLEEP)
    dag_src = dag_with(max_active_runs=2)
    unpause()
    with patched_file(INGEST_PATH, ingest_src), patched_file(DAG_PATH, dag_src):
        trigger("chaos_06_overlap_a")
        time.sleep(8)
        trigger("chaos_06_overlap_b")
        state_a = wait_dag_terminal("chaos_06_overlap_a", timeout=500)
        state_b = wait_dag_terminal("chaos_06_overlap_b", timeout=500)
    after = snapshot()
    published = [r for r in after["releases"] if r["status"] == "published"]
    superseded = [r for r in after["releases"] if r["status"] == "superseded"]
    extra = {
        "dag_a": state_a,
        "dag_b": state_b,
        "task_a": task_states("chaos_06_overlap_a"),
        "task_b": task_states("chaos_06_overlap_b"),
        "published": published,
        "superseded": superseded,
        "active": after["active"],
        "active_seq": next(
            (r["seq"] for r in after["releases"] if r["release_id"] == after["active"]),
            None,
        ),
    }
    print_report("6 overlapping DAG runs", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
