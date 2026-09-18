"""Phase A gap: do the real models actually emit the hand-calculated numbers?

Phase A's expected_mart_*.csv files were computed from the SQL contracts by
hand. Until this file, nothing loaded the fixture CSV, ran dbt, published,
and compared marts.v_* to those files. A wrong GROUP BY or a quiet fan-out
could have sat in main forever.

This test does **not** stand in for dbt with INSERT of the expected CSVs
(that is what test_publication_safety.py does, on purpose). It runs dbt.

If a cell does not match, this test must fail. Do not edit the expected
CSVs to silence it — the fixtures are the contract, not the warehouse.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from psycopg2.extras import RealDictCursor

from conftest import FIXTURES, ROOT
from release import mark_candidate, open_release, publish_release, record_source_checksum

# Stable id so the dbt --vars value is readable in logs and in the mart rows.
# open_release is called with this id so the mart FK to pipeline_releases holds.
PHASE_C_RELEASE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

DBT_DIR = ROOT / "dbt_project"

# Money is ROUND(..., 2) in SQL. 0.05 lets through a one-cent interpolation
# wiggle without hiding a wrong salary (thousands off).
MONEY_TOL = Decimal("0.05")
# Percents are ROUND(..., 1).
PCT_TOL = Decimal("0.15")

SALARY_KEYS = (
    "country",
    "experience_band",
    "dev_type",
    "remote_work",
    "org_size",
)
SALARY_NUMS = {
    "respondent_count": Decimal("0"),
    "avg_salary": MONEY_TOL,
    "median_salary": MONEY_TOL,
    "p25_salary": MONEY_TOL,
    "p75_salary": MONEY_TOL,
    "min_salary": MONEY_TOL,
    "max_salary": MONEY_TOL,
}

AI_KEYS = ("country", "dev_type", "ai_select", "ai_sent", "ai_threat")
AI_NUMS = {
    "respondent_count": Decimal("0"),
    "avg_job_satisfaction": MONEY_TOL,
    "pct_see_ai_as_threat": PCT_TOL,
}

TECH_KEYS = ("tech_type", "tech_name")
TECH_NUMS = {
    "total_users": Decimal("0"),
    "want_to_continue_count": Decimal("0"),
    "retention_rate_pct": PCT_TOL,
    "usage_rank": Decimal("0"),
}


def _run_dbt(subcommand: str, release_id: str, survey_year: int = 2024) -> None:
    """Call the real dbt CLI (run or test) with this release_id and year.

    Uses the same --profiles-dir / --target / --vars shape as the DAG.
    Fails the pytest with dbt's stdout/stderr on a non-zero exit.
    """
    dbt_bin = shutil.which("dbt")
    if dbt_bin is None:
        # pytest is often `venv/Scripts/python -m pytest` without Scripts on PATH.
        sibling = Path(sys.executable).resolve().parent / (
            "dbt.exe" if os.name == "nt" else "dbt"
        )
        if sibling.is_file():
            dbt_bin = str(sibling)
    if dbt_bin is None:
        pytest.fail(
            "dbt is not on PATH. Install the same pin CI uses: "
            "pip install dbt-postgres==1.7.0"
        )
    vars_json = json.dumps({"release_id": release_id, "survey_year": int(survey_year)})
    cmd = [
        dbt_bin,
        subcommand,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(DBT_DIR),
        "--target",
        "prod",
        "--vars",
        vars_json,
    ]
    env = os.environ.copy()
    env.setdefault("SURVEY_DB_HOST", "localhost")
    env.setdefault("SURVEY_DB_PORT", "5432")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(DBT_DIR),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"dbt {subcommand} failed (exit {proc.returncode}) for "
            f"release_id={release_id}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )


def _csv_rows(path: Path) -> list[dict[str, str | None]]:
    """Load an expected mart CSV. Blank cells are None (SQL NULL)."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = []
        for raw in csv.DictReader(f):
            rows.append({k: (None if v == "" else v) for k, v in raw.items()})
        return rows


def _view_rows(conn, sql: str) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _cell(value) -> str:
    if value is None:
        return "NULL"
    return str(value)


def _compare_mart(
    *,
    name: str,
    actual: list[dict],
    expected: list[dict],
    keys: tuple[str, ...],
    numeric: dict[str, Decimal],
) -> list[str]:
    """Row-for-row compare on grain keys. Returns human-readable mismatch lines.

    Order in the view is not part of the contract (the mart SQLs do not all
    ORDER BY). We match on the grain, then check every expected column.
    """
    problems: list[str] = []

    def key_of(row: dict) -> tuple:
        # CSV and Postgres both become strings so "United States" matches.
        return tuple(None if row.get(k) in ("", None) else str(row.get(k)) for k in keys)

    exp_map = {key_of(row): row for row in expected}
    act_map = {key_of(row): row for row in actual}

    extra = sorted(act_map.keys() - exp_map.keys())
    missing = sorted(exp_map.keys() - act_map.keys())
    if extra:
        problems.append(f"{name}: unexpected extra row(s) in the view: {extra}")
    if missing:
        problems.append(f"{name}: expected row(s) missing from the view: {missing}")

    for key in sorted(exp_map.keys() & act_map.keys()):
        exp_row = exp_map[key]
        act_row = act_map[key]
        for col, tol in numeric.items():
            exp_n = _as_decimal(exp_row.get(col))
            act_n = _as_decimal(act_row.get(col))
            if exp_n is None and act_n is None:
                continue
            if exp_n is None or act_n is None:
                problems.append(
                    f"{name} {key} {col}: expected {_cell(exp_n)}, got {_cell(act_n)}"
                )
                continue
            if abs(act_n - exp_n) > tol:
                problems.append(
                    f"{name} {key} {col}: expected {exp_n}, got {act_n} "
                    f"(delta {act_n - exp_n}, tol {tol})"
                )
        text_cols = [
            c
            for c in exp_row
            if c not in numeric and c not in keys and c != "release_id"
        ]
        for col in text_cols:
            exp_t = exp_row.get(col)
            act_t = act_row.get(col)
            act_s = None if act_t is None else str(act_t)
            exp_s = None if exp_t is None else str(exp_t)
            if act_s != exp_s:
                problems.append(
                    f"{name} {key} {col}: expected {exp_s!r}, got {act_s!r}"
                )
    if len(actual) != len(expected):
        problems.append(
            f"{name}: view has {len(actual)} row(s), expected CSV has {len(expected)}"
        )
    return problems


def test_published_views_match_phase_a_hand_calculated_csvs(warehouse):
    """Load fixture raw → real dbt → publish → marts.v_* == expected_mart_*.csv."""
    conn = warehouse
    rid = PHASE_C_RELEASE_ID
    open_release(release_id=rid, survey_year=2024)
    record_source_checksum(rid)
    _run_dbt("run", rid, survey_year=2024)
    _run_dbt("test", rid, survey_year=2024)
    mark_candidate(rid)
    assert publish_release(rid) == "published"

    problems: list[str] = []
    problems += _compare_mart(
        name="v_salary_analytics",
        actual=_view_rows(conn, "SELECT * FROM marts.v_salary_analytics"),
        expected=_csv_rows(FIXTURES / "expected_mart_salary_analytics.csv"),
        keys=SALARY_KEYS,
        numeric=SALARY_NUMS,
    )
    problems += _compare_mart(
        name="v_ai_sentiment",
        actual=_view_rows(conn, "SELECT * FROM marts.v_ai_sentiment"),
        expected=_csv_rows(FIXTURES / "expected_mart_ai_sentiment.csv"),
        keys=AI_KEYS,
        numeric=AI_NUMS,
    )
    problems += _compare_mart(
        name="v_tech_adoption",
        actual=_view_rows(conn, "SELECT * FROM marts.v_tech_adoption"),
        expected=_csv_rows(FIXTURES / "expected_mart_tech_adoption.csv"),
        keys=TECH_KEYS,
        numeric=TECH_NUMS,
    )

    if problems:
        pytest.fail(
            "Real dbt output did not match Phase A hand-calculated fixtures. "
            "The expected CSVs were not edited. Details:\n- "
            + "\n- ".join(problems)
        )

