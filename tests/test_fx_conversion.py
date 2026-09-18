"""Currency normalization: real FX rates, honest NULLs for unmapped currencies.

comp_total_raw was always self-reported compensation in the respondent's own
currency (see docs/engineering_journal.md entry 1: comp_total_usd never
actually converted anything). This adds comp_total_usd_converted, computed
from a real ECB-sourced FX rate for a single fixed date per survey year (the
same fixed-date approach Stack Overflow's own methodology uses). A currency
with no row in seeds/fx_rates_to_usd.csv must come out NULL, never a guessed
or default rate — that is the property this file exists to protect.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import FIXTURES, apply_migrate, load_raw_fixtures, wipe_warehouse
from db import get_connection
from ingest_survey import run as ingest_run
from release import mark_candidate, open_release, publish_release, record_source_checksum
from test_mart_correctness import PHASE_C_RELEASE_ID, _run_dbt

MONEY_TOL = Decimal("0.02")


def _row(conn, response_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT currency, currency_code, comp_total_raw, comp_total_usd_converted
            FROM staging.stg_survey_responses
            WHERE survey_year = 2024 AND response_id = %s
            """,
            (response_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"{response_id} not found in staging.stg_survey_responses"
    return {
        "currency": row[0],
        "currency_code": row[1],
        "comp_total_raw": row[2],
        "comp_total_usd_converted": row[3],
    }


def _close(actual, expected) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return abs(Decimal(str(actual)) - Decimal(str(expected))) <= MONEY_TOL


@pytest.fixture(scope="module")
def fx_release(pg_available):
    """One real dbt run over the 2024 fixture (which now includes R008-R013
    covering USD, INR, CAD, EUR, and the unmapped XYZ), so every test in this
    file reads the same already-built staging rows instead of re-running dbt
    per test.

    Uses pg_available rather than the function-scoped warehouse fixture so
    this can stay module-scoped. Same migrate/wipe/connect pattern as
    conftest.warehouse.
    """
    conn = get_connection()
    conn.autocommit = True
    apply_migrate(conn)
    wipe_warehouse(conn)
    load_raw_fixtures(conn)
    rid = PHASE_C_RELEASE_ID
    open_release(release_id=rid, survey_year=2024)
    record_source_checksum(rid)
    _run_dbt("seed", rid, survey_year=2024)
    _run_dbt("run", rid, survey_year=2024)
    _run_dbt("test", rid, survey_year=2024)
    mark_candidate(rid)
    assert publish_release(rid) == "published"
    yield conn
    wipe_warehouse(conn)
    conn.close()


def test_seed_loaded_real_eur_rate(fx_release):
    """The 2023 EUR rate this file's mart-level test depends on is really in
    the warehouse, not just in the CSV on disk (dbt seed actually ran)."""
    conn = fx_release
    with conn.cursor() as cur:
        cur.execute(
            "SELECT usd_per_unit FROM dwh.fx_rates_to_usd "
            "WHERE survey_year = 2023 AND currency_code = 'EUR'"
        )
        row = cur.fetchone()
    assert row is not None, "fx_rates_to_usd seed did not load 2023 EUR"
    assert _close(row[0], Decimal("1.07629882"))


def test_usd_currency_converts_at_parity(fx_release):
    """USD respondents: comp_total_usd_converted == comp_total_raw (rate 1.0)."""
    row = _row(fx_release, "R001")
    assert row["currency_code"] == "USD"
    assert _close(row["comp_total_usd_converted"], row["comp_total_raw"])


def test_inr_currency_converts_with_real_2024_rate(fx_release):
    """R008: 180000 INR at the real 2024-06-11 ECB rate -> 2153.88 USD."""
    row = _row(fx_release, "R008")
    assert row["currency_code"] == "INR"
    assert row["comp_total_raw"] is not None
    assert _close(row["comp_total_usd_converted"], Decimal("2153.88"))


def test_cad_currency_converts_with_real_2024_rate(fx_release):
    """R009/R010/R011: three real CAD amounts at the real 2024-06-11 rate."""
    expected = {
        "R009": Decimal("36297.64"),
        "R010": Decimal("4355716.86"),
        "R011": Decimal("65335.75"),
    }
    for response_id, usd in expected.items():
        row = _row(fx_release, response_id)
        assert row["currency_code"] == "CAD"
        assert _close(row["comp_total_usd_converted"], usd), (
            f"{response_id}: expected {usd}, got {row['comp_total_usd_converted']}"
        )


def test_eur_currency_converts_with_real_2024_rate(fx_release):
    """R012: 80000 EUR at the real 2024-06-11 rate -> 85839.67 USD."""
    row = _row(fx_release, "R012")
    assert row["currency_code"] == "EUR"
    assert _close(row["comp_total_usd_converted"], Decimal("85839.67"))


def test_unmapped_currency_is_null_not_guessed(fx_release):
    """R013: currency 'XYZ' has no seed row. comp_total_usd_converted must be
    NULL. comp_total_raw must still be preserved untouched. This is the
    property the whole feature exists to guarantee: an unmapped currency
    degrades to an honest NULL, never a default or invented rate.
    """
    row = _row(fx_release, "R013")
    assert row["currency_code"] == "XYZ"
    assert row["comp_total_raw"] == Decimal("90000")
    assert row["comp_total_usd_converted"] is None


def test_salary_mart_usd_columns_match_real_eur_conversion(fx_release):
    """The published 2023 Germany/EUR salary cell's USD columns are the raw
    columns times the real 2023-06-02 EUR rate, rounded the same way the SQL
    rounds (ROUND(..., 2))."""
    conn = fx_release
    rid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    ingest_run(
        survey_year=2023,
        source_path=str(FIXTURES / "raw_survey_responses_2023.csv"),
    )
    open_release(release_id=rid, survey_year=2023)
    record_source_checksum(rid)
    _run_dbt("seed", rid, survey_year=2023)
    _run_dbt("run", rid, survey_year=2023)
    _run_dbt("test", rid, survey_year=2023)
    mark_candidate(rid)
    assert publish_release(rid) == "published"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fx_converted_count, avg_salary_usd, median_salary_usd,
                   p25_salary_usd, p75_salary_usd, min_salary_usd, max_salary_usd
            FROM marts.v_salary_analytics
            WHERE survey_year = 2023 AND country = 'Germany'
            """
        )
        row = cur.fetchone()
    assert row is not None, "expected the Germany/EUR 2023 salary cell to be published"
    fx_count, avg_usd, median_usd, p25_usd, p75_usd, min_usd, max_usd = row
    assert fx_count == 6
    assert _close(avg_usd, Decimal("113011.38"))
    assert _close(median_usd, Decimal("113011.38"))
    assert _close(p25_usd, Decimal("99557.64"))
    assert _close(p75_usd, Decimal("126465.11"))
    assert _close(min_usd, Decimal("86103.91"))
    assert _close(max_usd, Decimal("139918.85"))
