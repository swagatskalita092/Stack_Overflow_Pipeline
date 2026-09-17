"""Injection 4: real dbt test failure on a live DAG run (not the pytest harness).

Temporarily adds a schema.yml test that cannot pass, so dbt_test_models fails.
publish_release must not run; the previously published release must stay live.
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

SCHEMA = ROOT / "dbt_project" / "models" / "marts" / "schema.yml"

FAILING_TEST = """
      - name: chaos_phase_d_must_fail
        description: "Phase D injection — always fails. Removed after the run."
        tests:
          - dbt_utils.expression_is_true:
              expression: "1 = 0"
"""

# dbt_utils is not a package of this project. Use a generic test instead:
# a not_null on a column we know can be null (ai_threat on the AI mart) would
# not reliably fail. A SQL test file is the most honest "downstream test".

SQL_TEST = ROOT / "dbt_project" / "tests" / "chaos_phase_d_always_fail.sql"
SQL_BODY = """
-- Phase D injection: a dbt test that always fails. Deleted after the run.
select 1 as should_be_empty where 1 = 1
"""


def main() -> int:
    zip_path = CHAOS_DATA / "fixture_survey.zip"
    write_survey_zip(zip_path, fixture_survey_csv_text(False))
    run_id = "chaos_04_dbt_test_failure"
    before = snapshot()
    patched = ingest_from_local_zip_source(zip_path)
    SQL_TEST.parent.mkdir(parents=True, exist_ok=True)
    unpause()
    with patched_file(INGEST_PATH, patched):
        SQL_TEST.write_text(SQL_BODY, encoding="utf-8")
        try:
            trigger(run_id)
            dag_state = wait_dag_terminal(run_id, timeout=400)
        finally:
            if SQL_TEST.exists():
                SQL_TEST.unlink()
    after = snapshot()
    states = task_states(run_id)
    extra = {
        "dag_state": dag_state,
        "task_states": states,
        "publish_ran": states.get("publish_release")
        not in (None, "upstream_failed", "skipped"),
        "publish_state": states.get("publish_release"),
        "active_unchanged": after["active"] == before["active"],
    }
    print_report("4 real dbt test failure via DAG", before, after, extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
