"""Injection 3: kill Postgres while dbt_run_models is running.

Confirms a partial mart write cannot become the live view (views filter to
active_release, which publish has not moved yet) and that the warehouse
comes back after Postgres restarts.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "chaos"))

from _common import (  # noqa: E402
    CHAOS_DATA,
    INGEST_PATH,
    compose,
    fixture_survey_csv_text,
    ingest_from_local_zip_source,
    patched_file,
    print_report,
    snapshot,
    task_states,
    trigger,
    unpause,
    wait_dag_terminal,
    wait_until_running,
    write_survey_zip,
)


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(False))
    run_id = "chaos_03_kill_postgres_mid_dbt"
    before = snapshot()
    patched = ingest_from_local_zip_source(zip_path)
    unpause()
    with patched_file(INGEST_PATH, patched):
        trigger(run_id)
        wait_until_running(run_id, "dbt_run_models", timeout=300)
        time.sleep(1)
        compose("kill", "postgres", check=False)
        time.sleep(5)
        compose("start", "postgres", check=False)
        # Wait until pg_isready inside the container.
        for _ in range(30):
            ready = compose(
                "exec", "-T", "postgres", "pg_isready", "-U", "airflow", check=False
            )
            if ready.returncode == 0:
                break
            time.sleep(2)
        dag_state = wait_dag_terminal(run_id, timeout=300)
    after = snapshot()
    extra = {
        "dag_state": dag_state,
        "task_states": task_states(run_id),
        "active_unchanged": after["active"] == before["active"],
        "view_ids_unchanged": after["view_ids"] == before["view_ids"],
    }
    print_report("3 postgres killed mid dbt_run_models", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
