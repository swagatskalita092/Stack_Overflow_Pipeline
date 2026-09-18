"""Shared helpers for Phase D chaos scripts.

These talk to the live Compose stack: Airflow metadata in database
``airflow``, warehouse in ``survey_db``. Chaos scripts patch bind-mounted
files for the duration of one injection and restore them in ``finally``.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DAG_PATH = ROOT / "airflow" / "dags" / "stackoverflow_pipeline_dag.py"
INGEST_PATH = SCRIPTS / "ingest_survey.py"
FIXTURE_CSV = ROOT / "tests" / "fixtures" / "raw_survey_responses.csv"
CHAOS_DATA = ROOT / "data" / "chaos"
DAG_ID = "stackoverflow_survey_pipeline"

# Warehouse column → public-survey PascalCase (inverse of ingest COLUMN_RENAME).
PASCAL = {
    "response_id": "ResponseId",
    "main_branch": "MainBranch",
    "age": "Age",
    "remote_work": "RemoteWork",
    "ed_level": "EdLevel",
    "years_code": "YearsCode",
    "years_code_pro": "YearsCodePro",
    "dev_type": "DevType",
    "org_size": "OrgSize",
    "country": "Country",
    "currency": "Currency",
    "comp_total": "CompTotal",
    "language_have_worked": "LanguageHaveWorkedWith",
    "language_want_work": "LanguageWantToWorkWith",
    "database_have_worked": "DatabaseHaveWorkedWith",
    "database_want_work": "DatabaseWantToWorkWith",
    "platform_have_worked": "PlatformHaveWorkedWith",
    "platform_want_work": "PlatformWantToWorkWith",
    "ai_select": "AISelect",
    "ai_sent": "AISent",
    "ai_threat": "AIThreat",
    "job_sat": "JobSat",
    "industry": "Industry",
}

# Dropped on ingest; still required so pandas sees a survey-shaped CSV.
PASCAL_OPTIONAL = ["loaded_at"]


def compose(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run ``docker compose`` in the project root."""
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def scheduler(*airflow_args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """``airflow`` CLI inside the scheduler container."""
    return compose(
        "exec",
        "-T",
        "airflow-scheduler",
        "airflow",
        *airflow_args,
        check=check,
        timeout=timeout,
    )


def connect_warehouse():
    return psycopg2.connect(
        host=os.getenv("SURVEY_DB_HOST", "localhost"),
        port=int(os.getenv("SURVEY_DB_PORT", "5432")),
        dbname="survey_db",
        user="airflow",
        password="airflow",
    )


def connect_airflow_meta():
    return psycopg2.connect(
        host=os.getenv("SURVEY_DB_HOST", "localhost"),
        port=int(os.getenv("SURVEY_DB_PORT", "5432")),
        dbname="airflow",
        user="airflow",
        password="airflow",
    )


def snapshot() -> dict:
    """Warehouse publication + raw row count. Used before/after each injection."""
    conn = connect_warehouse()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT survey_year, release_id::text FROM dwh.active_release ORDER BY survey_year"
            )
            active_rows = cur.fetchall()
            active_by_year = {int(r[0]): r[1] for r in active_rows}
            # Chaos scripts still read "active" as a single id. The weekly DAG
            # defaults to 2024; that year's pointer is the one they care about.
            active = active_by_year.get(2024)
            if active is None and len(active_by_year) == 1:
                active = next(iter(active_by_year.values()))
            cur.execute("SELECT COUNT(*) FROM raw.survey_responses")
            raw_n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM marts.v_salary_analytics")
            view_n = cur.fetchone()[0]
            cur.execute(
                "SELECT DISTINCT release_id::text FROM marts.v_salary_analytics"
            )
            view_ids = sorted(r[0] for r in cur.fetchall())
            cur.execute(
                """
                SELECT release_id::text, seq, status
                FROM dwh.pipeline_releases
                ORDER BY seq
                """
            )
            releases = [
                {"release_id": r[0], "seq": r[1], "status": r[2]}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT check_name, issue_type, row_count FROM dwh.dq_issues ORDER BY id"
            )
            dq = [
                {"check": r[0], "type": r[1], "count": r[2]} for r in cur.fetchall()
            ]
        return {
            "active": active,
            "active_by_year": active_by_year,
            "raw_n": raw_n,
            "view_n": view_n,
            "view_ids": view_ids,
            "releases": releases,
            "dq": dq,
        }
    finally:
        conn.close()


def task_states(run_id: str) -> dict[str, str]:
    """Airflow task_instance.state keyed by task_id. Empty if the run is unknown."""
    conn = connect_airflow_meta()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, state
                FROM task_instance
                WHERE dag_id = %s AND run_id = %s
                """,
                (DAG_ID, run_id),
            )
            return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def wait_for_task(run_id: str, task_id: str, timeout: int = 300) -> str:
    """Block until ``task_id`` is no longer queued/scheduled/running, or timeout."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        states = task_states(run_id)
        last = states.get(task_id) or ""
        if last in {"success", "failed", "up_for_retry", "upstream_failed", "skipped"}:
            return last
        time.sleep(2)
    raise TimeoutError(f"{run_id}/{task_id} still {last!r} after {timeout}s")


def wait_until_running(run_id: str, task_id: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task_states(run_id).get(task_id) == "running":
            return
        time.sleep(1)
    raise TimeoutError(f"{run_id}/{task_id} never reached running")


def wait_dag_terminal(run_id: str, timeout: int = 600) -> str:
    """Wait until every task is in a terminal state. Returns dag-run state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = connect_airflow_meta()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM dag_run WHERE dag_id = %s AND run_id = %s",
                    (DAG_ID, run_id),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0] in {"success", "failed"}:
            return row[0]
        time.sleep(3)
    raise TimeoutError(f"dag run {run_id} not terminal after {timeout}s")


def unpause() -> None:
    scheduler("dags", "unpause", DAG_ID)


def trigger(run_id: str) -> None:
    scheduler("dags", "trigger", DAG_ID, "--run-id", run_id)


def kill_pidfile() -> str:
    """SIGKILL the PID a chaos hook wrote to data/chaos/killme.pid."""
    path = ROOT / "data" / "chaos" / "killme.pid"
    if not path.is_file():
        return "no killme.pid"
    pid = path.read_text(encoding="utf-8").strip()
    if not pid.isdigit():
        return f"bad pidfile {pid!r}"
    proc = compose(
        "exec",
        "-T",
        "airflow-scheduler",
        "python",
        "-c",
        f"import os; os.kill({pid}, 9); print('KILLED {pid}')",
        check=False,
    )
    return f"pidfile={pid} " + (proc.stdout or "") + (proc.stderr or "")


@contextmanager
def patched_file(path: Path, new_text: str):
    """Replace a bind-mounted file, restore original bytes on exit."""
    original = path.read_text(encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    try:
        yield
    finally:
        path.write_text(original, encoding="utf-8")


def fixture_survey_csv_text(truncate_mid_row: bool = False) -> str:
    """Phase A fixture rewritten as a public-survey (PascalCase) CSV."""
    with FIXTURE_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out_cols = [PASCAL[c] for c in rows[0] if c in PASCAL]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=out_cols, lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow({PASCAL[c]: row[c] for c in row if c in PASCAL})
    text = buf.getvalue()
    if truncate_mid_row:
        # Cut the last line in half so the file is not a valid CSV record.
        lines = text.splitlines(keepends=True)
        if len(lines) >= 2:
            last = lines[-1]
            lines[-1] = last[: max(1, len(last) // 2)]
        text = "".join(lines)
    return text


def write_survey_zip(path: Path, csv_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("survey_results_public.csv", csv_text)


def container_path(host_path: Path) -> str:
    """Host path under the repo → the bind-mount path Airflow sees."""
    rel = host_path.resolve().relative_to(ROOT.resolve())
    return "/opt/airflow/" + rel.as_posix()


def ingest_from_local_zip_source(zip_path: Path, extra_load_lines: str = "") -> str:
    """ingest_survey.py with _download_zip reading a local file instead of the CDN.

    extra_load_lines is spliced into _load_to_postgres after the year-scoped
    DELETE (for the mid-delete kill window).
    """
    src = INGEST_PATH.read_text(encoding="utf-8")
    posix = container_path(zip_path)
    old_dl = '''    url = survey_data_url(survey_year)
    logger.info("Downloading survey data from %s", url)
    resp = requests.get(url, timeout=180, allow_redirects=True)
    resp.raise_for_status()
    logger.info("Downloaded %s bytes", len(resp.content))
    return resp.content'''
    new_dl = f'''    path = {posix!r}
    logger.info("CHAOS: reading local survey ZIP from %s", path)
    with open(path, "rb") as fh:
        content = fh.read()
    logger.info("Read %s bytes from local ZIP", len(content))
    return content'''
    if old_dl not in src:
        raise RuntimeError("ingest_survey.py download block changed; update chaos patch")
    src = src.replace(old_dl, new_dl, 1)
    if extra_load_lines:
        marker = (
            '            logger.info(\n'
            '                "Deleted existing raw.survey_responses rows for survey_year=%s",\n'
            '                survey_year,\n'
            '            )\n'
        )
        if marker not in src:
            raise RuntimeError("year-scoped delete log line missing; update chaos patch")
        src = src.replace(marker, marker + extra_load_lines, 1)
    return src


def dag_with(*, retry_seconds: int | None = None, max_active_runs: int | None = None) -> str:
    src = DAG_PATH.read_text(encoding="utf-8")
    if retry_seconds is not None:
        src = src.replace(
            '"retry_delay": timedelta(minutes=5),',
            f'"retry_delay": timedelta(seconds={retry_seconds}),',
            1,
        )
    if max_active_runs is not None:
        src = src.replace(
            '    catchup=False,\n',
            f'    catchup=False,\n    max_active_runs={max_active_runs},\n',
            1,
        )
    return src


def print_report(title: str, before: dict, after: dict, extra: dict) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    print("BEFORE", {k: before[k] for k in ("active", "raw_n", "view_n", "view_ids")})
    print("AFTER ", {k: after[k] for k in ("active", "raw_n", "view_n", "view_ids")})
    print("RELEASES", after["releases"])
    print("DQ", after["dq"])
    for k, v in extra.items():
        print(k, v)
    print()
