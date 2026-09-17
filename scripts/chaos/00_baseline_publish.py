"""Establish one published fixture release so chaos tests have a live baseline.

Uses the local Phase A fixture ZIP (not the CDN) through a real DAG run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "chaos"))

from _common import (  # noqa: E402
    CHAOS_DATA,
    INGEST_PATH,
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


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(False))
    run_id = "chaos_00_baseline_fixture"
    before = snapshot()
    patched = ingest_from_local_zip_source(zip_path)
    unpause()
    with patched_file(INGEST_PATH, patched):
        trigger(run_id)
        dag_state = wait_dag_terminal(run_id, timeout=400)
    after = snapshot()
    print_report(
        "0 baseline fixture publish",
        before,
        after,
        {"dag_state": dag_state, "task_states": task_states(run_id)},
    )
    if dag_state != "success" or after["active"] is None:
        print("baseline did not publish; aborting chaos suite", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
