"""Injection 5: SIGKILL after publish_release commits, before Airflow success.

Patches publish_release to sleep after the DB commit. The killer SIGKILLs the
task process during that sleep. Airflow should retry; the retry must hit
already_active and not double-publish.
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
    SCRIPTS,
    dag_with,
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

RELEASE_PATH = SCRIPTS / "release.py"


def patched_release() -> str:
    src = RELEASE_PATH.read_text(encoding="utf-8")
    marker = '        logger.info("Published release %s", release_id)\n        return "published"\n'
    insert = (
        '        logger.info("Published release %s", release_id)\n'
        '        import os as _chaos_os, time as _chaos_time\n'
        '        _pid_path = "/opt/airflow/data/chaos/killme.pid"\n'
        '        with open(_pid_path, "w", encoding="utf-8") as _fh:\n'
        '            _fh.write(str(_chaos_os.getpid()))\n'
        '        logger.info("CHAOS: sleeping after publish commit pid=%s", _chaos_os.getpid())\n'
        '        _chaos_time.sleep(25)\n'
        '        return "published"\n'
    )
    if marker not in src:
        raise RuntimeError("publish return site missing; update chaos patch")
    return src.replace(marker, insert, 1)


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(False))
    run_id = "chaos_05_kill_after_publish"
    before = snapshot()
    ingest_src = ingest_from_local_zip_source(zip_path)
    dag_src = dag_with(retry_seconds=15)
    rel_src = patched_release()
    unpause()
    with patched_file(INGEST_PATH, ingest_src), patched_file(DAG_PATH, dag_src), patched_file(
        RELEASE_PATH, rel_src
    ):
        trigger(run_id)
        wait_until_running(run_id, "publish_release", timeout=400)
        pid_path = ROOT / "data" / "chaos" / "killme.pid"
        for _ in range(40):
            if pid_path.is_file():
                break
            time.sleep(0.5)
        kill_out = kill_pidfile()
        dag_state = wait_dag_terminal(run_id, timeout=240)
    after = snapshot()
    extra = {
        "kill": kill_out,
        "dag_state": dag_state,
        "task_states": task_states(run_id),
        "active_moved": after["active"] != before["active"] and after["active"] is not None,
        "published_count": sum(1 for r in after["releases"] if r["status"] == "published"),
        "active_is_singleton": True,  # snapshot already queried one row
    }
    print_report("5 kill after publish commit, before Airflow success", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
