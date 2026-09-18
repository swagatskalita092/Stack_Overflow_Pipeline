"""Download the Stack Overflow Developer Survey, clean it, load Postgres.

Why this script exists
----------------------
Airflow should not scrape a 65k-row CSV by hand. One function (`run`) does
download → unzip in memory → keep the columns we model → replace survey
sentinels with SQL NULL → replace that *year's* rows in the raw table.
The rest of the pipeline (DQ, dbt) assumes `raw.survey_responses` looks
like this extract, not like the 114-column public file.

survey_year is explicit (CLI `--year` / env `SURVEY_YEAR`). We do not
guess it from the filename or a CSV column — those are fragile across
archive layouts. Default is 2024 so an unconfigured weekly DAG still
loads the year this pipeline started on.
"""

import argparse
import io
import logging
import os
import zipfile
from pathlib import Path

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Official extracts. survey.stackoverflow.co/datasets/...zip is HTTP 404
# (confirmed Phase D). The survey site's "Data & files" list points at
# StackExchange/Survey on GitHub (results.csv, Git LFS). A later year is a
# new URL, not a silent overwrite of this one.
SURVEY_DATA_URLS = {
    2024: (
        "https://github.com/StackExchange/Survey/raw/refs/heads/main/"
        "packages/archive/2024/results.csv"
    ),
    2023: (
        "https://github.com/StackExchange/Survey/raw/refs/heads/main/"
        "packages/archive/2023/results.csv"
    ),
}
# Default / chaos-patch target. _download_source uses SURVEY_DATA_URLS[year].
SURVEY_DATA_URL = SURVEY_DATA_URLS[2024]
# Kept so older comments / chaos patches that say SURVEY_ZIP_URL still resolve.
SURVEY_ZIP_URL = SURVEY_DATA_URL

DEFAULT_SURVEY_YEAR = 2024

# PascalCase names in the CSV → snake_case names in raw.survey_responses.
# Anything not in this map is dropped on purpose (we do not load all 114 columns).
COLUMN_RENAME = {
    "ResponseId": "response_id",
    "MainBranch": "main_branch",
    "Age": "age",
    "RemoteWork": "remote_work",
    "EdLevel": "ed_level",
    "YearsCode": "years_code",
    "YearsCodePro": "years_code_pro",
    "DevType": "dev_type",
    "OrgSize": "org_size",
    "Country": "country",
    "Currency": "currency",
    "CompTotal": "comp_total",
    "LanguageHaveWorkedWith": "language_have_worked",
    "LanguageWantToWorkWith": "language_want_work",
    "DatabaseHaveWorkedWith": "database_have_worked",
    "DatabaseWantToWorkWith": "database_want_work",
    "PlatformHaveWorkedWith": "platform_have_worked",
    "PlatformWantToWorkWith": "platform_want_work",
    "AISelect": "ai_select",
    "AISent": "ai_sent",
    "AIThreat": "ai_threat",
    "JobSat": "job_sat",
    "Industry": "industry",
}

# Strings the survey uses for "no answer". They must become SQL NULL, not the
# text "NA", or staging's CAST(comp_total AS NUMERIC) will fail.
SENTINEL_VALUES = {"NA", "N/A", "nan", "NaN", "None", ""}

# Columns the public extract does not have in a given year. Ingest still
# lands them as SQL NULL (the warehouse columns stay). DQ must not treat
# that known absence as a data-quality failure — see KNOWN_ABSENT_COLUMNS
# in dq_checks.py and docs/data_contracts.md.
KNOWN_ABSENT_SOURCE_COLUMNS = {
    2023: ("AIThreat", "JobSat"),
}


def resolve_survey_year(explicit=None) -> int:
    """Year this run is loading. CLI / caller wins, then SURVEY_YEAR, then 2024.

    Explicit on purpose: auto-detecting from a path or a CSV header is how
    a 2023 file would silently stamp 2024 (or the other way around).
    """
    if explicit is not None and str(explicit).strip() != "":
        return int(explicit)
    env = os.getenv("SURVEY_YEAR")
    if env is not None and str(env).strip() != "":
        return int(env)
    return DEFAULT_SURVEY_YEAR


def survey_data_url(survey_year: int) -> str:
    """Canonical GitHub archive URL for this year, or raise if we do not know it."""
    url = SURVEY_DATA_URLS.get(int(survey_year))
    if not url:
        known = ", ".join(str(y) for y in sorted(SURVEY_DATA_URLS))
        raise ValueError(
            f"no survey URL configured for survey_year={survey_year}; known: {known}"
        )
    return url


def _download_source(survey_year: int) -> bytes:
    """Pull the official extract (CSV today; ZIP still accepted).

    We keep the file in memory so the container does not need a writable data
    directory. timeout=180 covers the ~160 MB Git LFS CSV; a hung host should
    fail the task so Airflow retries rather than sit forever.
    """
    url = survey_data_url(survey_year)
    logger.info("Downloading survey data from %s", url)
    resp = requests.get(url, timeout=180, allow_redirects=True)
    resp.raise_for_status()
    logger.info("Downloaded %s bytes", len(resp.content))
    return resp.content


def _frame_from_download(blob: bytes) -> pd.DataFrame:
    """ZIP (legacy CDN) or bare CSV (current GitHub archive)."""
    if blob[:2] == b"PK":
        logger.info("Extracting survey_results_public.csv from ZIP")
        with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
            with zf.open("survey_results_public.csv") as f:
                df = pd.read_csv(f, low_memory=False)
    else:
        logger.info("Parsing downloaded CSV (not a ZIP)")
        df = pd.read_csv(io.BytesIO(blob), low_memory=False)
    logger.info("Read %d rows, %d columns", len(df), len(df.columns))
    return df


def _select_and_rename(df: pd.DataFrame) -> pd.DataFrame:
    """Keep COLUMN_RENAME keys that exist and rename them to warehouse names.

    A missing source column is a warning, not a crash: a future survey year
    might drop a field. Better to load the rest and let DQ / dbt tests say
    which mart broke than to fail ingest on one rename.
    """
    missing = [c for c in COLUMN_RENAME if c not in df.columns]
    if missing:
        logger.warning("Missing columns in CSV: %s", missing)
    cols = [c for c in COLUMN_RENAME if c in df.columns]
    out = df[cols].rename(columns=COLUMN_RENAME)
    logger.info("Selected and renamed %d columns", len(out.columns))
    return out


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Trim whitespace and turn survey sentinels into None (SQL NULL).

    Only object (string) columns are touched. Numeric pandas dtypes are left
    alone so we do not stringify real numbers. In-place on purpose: this frame
    is throwaway after the load.
    """
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace(
                {v: None for v in SENTINEL_VALUES}
            )
    logger.info("Cleaned string columns and sentinel values")
    return df


def _get_db_connection():
    """Open Postgres with the same env defaults the Airflow containers use.

    Defaults match docker-compose (host `postgres`, db `survey_db`). Override
    with SURVEY_DB_* when running the script on a laptop against localhost.
    """
    host = os.getenv("SURVEY_DB_HOST", "postgres")
    port = int(os.getenv("SURVEY_DB_PORT", "5432"))
    dbname = os.getenv("SURVEY_DB_NAME", "survey_db")
    user = os.getenv("SURVEY_DB_USER", "airflow")
    password = os.getenv("SURVEY_DB_PASSWORD", "airflow")
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
    )


def _load_to_postgres(df: pd.DataFrame, survey_year: int) -> None:
    """Replace this year's rows in raw.survey_responses, leave other years alone.

    DELETE ... WHERE survey_year = %s, then bulk INSERT. A 2023 load must not
    TRUNCATE a 2024 load that is already in the table (and the other way
    around). execute_values batches 1000 rows so we are not one INSERT per
    respondent. pandas NA/NaN become None so psycopg2 writes SQL NULL.
    """
    df = df.copy()
    df["survey_year"] = int(survey_year)
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM raw.survey_responses WHERE survey_year = %s",
                (int(survey_year),),
            )
            logger.info(
                "Deleted existing raw.survey_responses rows for survey_year=%s",
                survey_year,
            )

            columns = list(df.columns)
            table = "raw.survey_responses"
            cols_sql = ", ".join(f'"{c}"' for c in columns)
            insert_sql = f'INSERT INTO {table} ({cols_sql}) VALUES %s'

            # Convert to list of tuples; replace pd.NA/NaN with None
            rows = [
                tuple(None if pd.isna(v) else v for v in row)
                for row in df.to_numpy().tolist()
            ]
            execute_values(cur, insert_sql, rows, page_size=1000)
            conn.commit()
            logger.info(
                "Inserted %d rows into raw.survey_responses (survey_year=%s)",
                len(rows),
                survey_year,
            )
    finally:
        conn.close()


def run(survey_year=None, source_path=None) -> None:
    """Airflow entry point: download (or read a local file), clean, load one year."""
    year = resolve_survey_year(survey_year)
    path = source_path or os.getenv("SURVEY_DATA_PATH")
    logger.info("Starting survey ingestion survey_year=%s", year)
    if path:
        logger.info("Reading local survey file from %s", path)
        blob = Path(path).read_bytes()
    else:
        blob = _download_source(year)
    df = _frame_from_download(blob)
    df = _select_and_rename(df)
    df = _clean(df)
    _load_to_postgres(df, year)
    logger.info("Survey ingestion completed successfully (survey_year=%s)", year)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Load one Stack Overflow survey year into raw.survey_responses."
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Survey year to stamp on every row (default: SURVEY_YEAR or 2024).",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Local CSV/ZIP path. Default: download SURVEY_DATA_URLS[year].",
    )
    args = parser.parse_args()
    run(survey_year=args.year, source_path=args.source)
