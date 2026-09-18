"""Real-CDN end-to-end used by .github/workflows/real-dataset-check.yml.

This is the weekly / manual job, not PR CI. It downloads one public survey
year (default 2024), runs DQ + dbt + publish, and checks that marts.v_*
are non-empty for that year.

Do not call this from the required PR workflow: a down CDN must not block
a merge.

Pass --year 2023 to load the 2023 extract instead. 2023 AI views may have
NULL pct_see_ai_as_threat (column absent); salary and tech must still be
non-empty.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection  # noqa: E402
from dq_checks import run_checks  # noqa: E402
from ingest_survey import resolve_survey_year, run as ingest_run  # noqa: E402
from release import (  # noqa: E402
    mark_candidate,
    open_release,
    publish_release,
    record_source_checksum,
)


def _run_dbt(subcommand: str, release_id: str, survey_year: int) -> None:
    dbt_dir = _ROOT / "dbt_project"
    dbt_bin = shutil.which("dbt")
    if dbt_bin is None:
        sibling = Path(sys.executable).resolve().parent / (
            "dbt.exe" if os.name == "nt" else "dbt"
        )
        if sibling.is_file():
            dbt_bin = str(sibling)
    if dbt_bin is None:
        raise FileNotFoundError(
            "dbt is not on PATH. Install the same pin CI uses: "
            "pip install dbt-postgres==1.7.0"
        )
    target_dir = _ROOT / "data" / "dbt_target_e2e"
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        dbt_bin,
        subcommand,
        "--project-dir",
        str(dbt_dir),
        "--profiles-dir",
        str(dbt_dir),
        "--target-path",
        str(target_dir),
        "--target",
        "prod",
        "--vars",
        json.dumps({"release_id": release_id, "survey_year": int(survey_year)}),
    ]
    print("running", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(dbt_dir), env=os.environ.copy())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CDN ingest → dbt → publish one year.")
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Survey year (default: SURVEY_YEAR or 2024).",
    )
    args = parser.parse_args(argv)
    year = resolve_survey_year(args.year)

    rid = open_release(survey_year=year)
    print(f"opened release {rid} survey_year={year}", flush=True)

    ingest_run(survey_year=year)
    record_source_checksum(rid)
    run_checks(survey_year=year)
    _run_dbt("seed", rid, year)
    _run_dbt("run", rid, year)
    _run_dbt("test", rid, year)
    print("mark_candidate", mark_candidate(rid), flush=True)
    print("publish_release", publish_release(rid), flush=True)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM marts.v_salary_analytics
                WHERE survey_year = %s
                """,
                (year,),
            )
            salary_n = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM marts.v_tech_adoption
                WHERE survey_year = %s
                """,
                (year,),
            )
            tech_n = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM marts.v_ai_sentiment
                WHERE survey_year = %s
                """,
                (year,),
            )
            ai_n = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM raw.survey_responses
                WHERE survey_year = %s
                """,
                (year,),
            )
            raw_n = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM raw.survey_responses
                WHERE survey_year = %s AND ai_threat IS NULL
                """,
                (year,),
            )
            ai_threat_null = cur.fetchone()[0]
    finally:
        conn.close()

    print(
        f"published year={year} raw={raw_n} ai_threat_null={ai_threat_null} "
        f"view counts salary={salary_n} tech={tech_n} ai={ai_n}",
        flush=True,
    )
    # Real extracts must produce salary and tech cells. 2023 AI cells exist
    # (AISelect/AISent) even though threat rate is NULL.
    if salary_n < 1 or tech_n < 1 or ai_n < 1:
        print(
            f"expected non-empty published views for {year}, got salary={salary_n} "
            f"tech={tech_n} ai={ai_n}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
