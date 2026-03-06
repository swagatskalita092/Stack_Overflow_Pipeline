"""
Stack Overflow Developer Survey 2024 ingestion script.
Downloads survey ZIP, extracts CSV in memory, cleans data, and loads into raw.survey_responses.
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

SURVEY_ZIP_URL = (
    "https://cdn.sanity.io/files/jo7n4k8s/production/262f04c41d99fea692e0125c342e446782233fe4.zip/"
    "stack-overflow-developer-survey-2024.zip"
)

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

SENTINEL_VALUES = {"NA", "N/A", "nan", "NaN", "None", ""}


def _download_zip() -> bytes:
    """Download the survey ZIP from the CDN."""
    logger.info("Downloading survey ZIP from %s", SURVEY_ZIP_URL)
    resp = requests.get(SURVEY_ZIP_URL, timeout=60)
    resp.raise_for_status()
    logger.info("Downloaded %s bytes", len(resp.content))
    return resp.content


def _extract_csv_from_zip(zip_bytes: bytes) -> pd.DataFrame:
    """Extract survey_results_public.csv from ZIP in memory."""
    logger.info("Extracting survey_results_public.csv from ZIP")
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        with zf.open("survey_results_public.csv") as f:
            df = pd.read_csv(f, low_memory=False)
    logger.info("Read %d rows, %d columns", len(df), len(df.columns))
    return df


def _select_and_rename(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only required columns and rename to target names."""
    missing = [c for c in COLUMN_RENAME if c not in df.columns]
    if missing:
        logger.warning("Missing columns in CSV: %s", missing)
    cols = [c for c in COLUMN_RENAME if c in df.columns]
    out = df[cols].rename(columns=COLUMN_RENAME)
    logger.info("Selected and renamed %d columns", len(out.columns))
    return out


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace and replace sentinel values with None."""
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace(
                {v: None for v in SENTINEL_VALUES}
            )
    logger.info("Cleaned string columns and sentinel values")
    return df


def _get_db_connection():
    """Build connection from env with defaults."""
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
    """Truncate raw.survey_responses and bulk insert with execute_values."""
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
    """Entry point: download, extract, clean, and load survey data."""
    logger.info("Starting survey ingestion")
    zip_bytes = _download_zip()
    df = _extract_csv_from_zip(zip_bytes)
    df = _select_and_rename(df)
    df = _clean(df)
    _load_to_postgres(df)
    logger.info("Survey ingestion completed successfully")


if __name__ == "__main__":
    run()
