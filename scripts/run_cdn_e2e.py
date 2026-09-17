"""Real-CDN end-to-end used by .github/workflows/real-dataset-check.yml.

This is the weekly / manual job, not PR CI. It downloads the public 2024 ZIP,
runs DQ + dbt + publish, and checks that marts.v_* are non-empty.

Do not call this from the required PR workflow — a down CDN must not block
a merge.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection  # noqa: E402
from dq_checks import run_checks  # noqa: E402
from ingest_survey import run as ingest_run  # noqa: E402
from release import (  # noqa: E402
    mark_candidate,
    open_release,
    publish_release,
    record_source_checksum,
)


def _run_dbt(subcommand: str, release_id: str) -> None:
    dbt_dir = _ROOT / "dbt_project"
    cmd = [
        "dbt",
        subcommand,
        "--project-dir",
        str(dbt_dir),
        "--profiles-dir",
        str(dbt_dir),
        "--target",
        "prod",
        "--vars",
        '{"release_id": "%s"}' % release_id,
    ]
    print("running", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(dbt_dir), env=os.environ.copy())


def main() -> int:
    rid = open_release()
    print(f"opened release {rid}", flush=True)

    ingest_run()
    record_source_checksum(rid)
    run_checks()
    _run_dbt("run", rid)
    _run_dbt("test", rid)
    print("mark_candidate", mark_candidate(rid), flush=True)
    print("publish_release", publish_release(rid), flush=True)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM marts.v_salary_analytics")
            salary_n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM marts.v_tech_adoption")
            tech_n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM marts.v_ai_sentiment")
            ai_n = cur.fetchone()[0]
    finally:
        conn.close()

    print(
        f"published view counts salary={salary_n} tech={tech_n} ai={ai_n}",
        flush=True,
    )
    # Real 2024 extract must produce cells; zero would mean publish pointed
    # at an empty build (or the views are not filtering to active_release).
    if salary_n < 1 or tech_n < 1 or ai_n < 1:
        print(
            f"expected non-empty published views, got salary={salary_n} "
            f"tech={tech_n} ai={ai_n}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
