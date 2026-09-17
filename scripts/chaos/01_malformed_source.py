"""Injection 1: malformed / truncated survey ZIP before ingest.

Patches ingest to read a local ZIP whose CSV is cut mid-row (not the CDN).
Triggers a real DAG run. We want to know: clean fail? does total_rows_loaded
ever see the junk? does active_release move?
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
    zip_path = CHAOS_DATA / "truncated_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(truncate_mid_row=True))
    run_id = "chaos_01_malformed_source"
    before = snapshot()
    patched = ingest_from_local_zip_source(zip_path)
    unpause()
    with patched_file(INGEST_PATH, patched):
        trigger(run_id)
        dag_state = wait_dag_terminal(run_id, timeout=300)
    after = snapshot()
    extra = {
        "dag_state": dag_state,
        "task_states": task_states(run_id),
        "active_unchanged": after["active"] == before["active"],
    }
    print_report("1 malformed/truncated source", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
