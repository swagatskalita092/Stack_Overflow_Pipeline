"""Injection 2: kill the loader after TRUNCATE, before INSERT commits.

Phase B documented this as an open raw-layer gap. The real ingest wraps
TRUNCATE + INSERT in one transaction; this script sleeps 25s after TRUNCATE
so we can SIGKILL the task and observe whether Postgres rolls back or leaves
raw empty.
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
    fixture_survey_csv_text,
    ingest_from_local_zip_source,
    kill_pidfile,
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

SLEEP = """
            import os as _chaos_os, time as _chaos_time
            _pid_path = "/opt/airflow/data/chaos/killme.pid"
            with open(_pid_path, "w", encoding="utf-8") as _fh:
                _fh.write(str(_chaos_os.getpid()))
            logger.info("CHAOS: wrote pid %s to %s; sleeping after TRUNCATE", _chaos_os.getpid(), _pid_path)
            _chaos_time.sleep(25)
"""


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(truncate_mid_row=False))
    run_id = "chaos_02d_kill_mid_truncate"
    before = snapshot()
    pid_path = ROOT / "data" / "chaos" / "killme.pid"
    if pid_path.exists():
        pid_path.unlink()
    patched = ingest_from_local_zip_source(zip_path, extra_load_lines=SLEEP)
    unpause()
    with patched_file(INGEST_PATH, patched):
        trigger(run_id)
        wait_until_running(run_id, "ingest_raw_survey", timeout=180)
        pid_path = ROOT / "data" / "chaos" / "killme.pid"
        for _ in range(40):
            if pid_path.is_file():
                break
            time.sleep(0.5)
        kill_out = kill_pidfile()
        mid = snapshot()
        dag_state = wait_dag_terminal(run_id, timeout=400)
    after = snapshot()
    extra = {
        "kill": kill_out,
        "mid_after_kill": {
            "raw_n": mid["raw_n"],
            "active": mid["active"],
            "raw_emptied": mid["raw_n"] == 0,
            "raw_unchanged": mid["raw_n"] == before["raw_n"],
        },
        "dag_state": dag_state,
        "task_states": task_states(run_id),
        "raw_emptied": after["raw_n"] == 0,
        "raw_unchanged": after["raw_n"] == before["raw_n"],
        "active_unchanged": after["active"] == before["active"],
    }
    print_report("2 kill loader mid truncate-and-reload", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
