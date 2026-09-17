"""Postgres connection used by ingest, DQ, and the release publisher.

One function so a laptop run (localhost) and Compose (host `postgres`)
keep the same env knobs: SURVEY_DB_HOST/PORT/NAME/USER/PASSWORD.
"""

from __future__ import annotations

import os

import psycopg2


def get_connection():
    """Open a new connection. Caller closes it (or uses a `with` block)."""
    return psycopg2.connect(
        host=os.getenv("SURVEY_DB_HOST", "postgres"),
        port=int(os.getenv("SURVEY_DB_PORT", "5432")),
        dbname=os.getenv("SURVEY_DB_NAME", "survey_db"),
        user=os.getenv("SURVEY_DB_USER", "airflow"),
        password=os.getenv("SURVEY_DB_PASSWORD", "airflow"),
    )
