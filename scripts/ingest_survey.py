"""Download the Stack Overflow Developer Survey ZIP, clean it, load Postgres.

Why this script exists
----------------------
Airflow should not scrape a 65k-row CSV by hand. One function (`run`) does
download → unzip in memory → keep the columns we model → replace survey
sentinels with SQL NULL → replace the raw table. The rest of the pipeline
(DQ, dbt) assumes `raw.survey_responses` looks like this extract, not like
the 114-column public file.
"""

import io
import logging
import os
import zipfile

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

# Public 2024 ZIP. A later year is a new URL, not a silent overwrite of this one.
SURVEY_ZIP_URL = "https://survey.stackoverflow.co/datasets/stack-overflow-developer-survey-2024.zip"

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


def _download_zip() -> bytes:
    """Pull the ZIP bytes from the CDN.

    We keep the file in memory so the container does not need a writable data
    directory. timeout=60 is a network guard, not a size guess — a hung CDN
    should fail the task so Airflow retries rather than sit forever.
    """
    logger.info("Downloading survey ZIP from %s", SURVEY_ZIP_URL)
    resp = requests.get(SURVEY_ZIP_URL, timeout=60)
    resp.raise_for_status()
    logger.info("Downloaded %s bytes", len(resp.content))
    return resp.content


def _extract_csv_from_zip(zip_bytes: bytes) -> pd.DataFrame:
    """Open survey_results_public.csv inside the ZIP without writing to disk.

    The public ZIP also contains schema and README files. We only want the
    respondent CSV; anything else is ignored.
    """
    logger.info("Extracting survey_results_public.csv from ZIP")
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        with zf.open("survey_results_public.csv") as f:
            df = pd.read_csv(f, low_memory=False)
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


def _load_to_postgres(df: pd.DataFrame) -> None:
    """Replace raw.survey_responses with this frame (truncate, then bulk insert).

    Truncate first so a rerun cannot append a second copy of the same survey.
    execute_values batches 1000 rows so we are not one INSERT per respondent.
    pandas NA/NaN become None so psycopg2 writes SQL NULL.
    """
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE raw.survey_responses")
            logger.info("Truncated raw.survey_responses")

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
            logger.info("Inserted %d rows into raw.survey_responses", len(rows))
    finally:
        conn.close()


def run() -> None:
    """Airflow entry point: download, extract, clean, load. No DQ, no dbt."""
    logger.info("Starting survey ingestion")
    zip_bytes = _download_zip()
    df = _extract_csv_from_zip(zip_bytes)
    df = _select_and_rename(df)
    df = _clean(df)
    _load_to_postgres(df)
    logger.info("Survey ingestion completed successfully")


if __name__ == "__main__":
    run()
