"""
Stack Overflow Developer Survey pipeline DAG.

Weekly pipeline that:
1. Ingests the Stack Overflow Developer Survey 2024 CSV from the CDN into raw.survey_responses.
2. Runs data quality checks and logs results to dwh.dq_issues.
3. Runs dbt models (staging → intermediate → marts).
4. Runs dbt tests on the models.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator


def _run_ingest():
    import sys
    sys.path.insert(0, "/opt/airflow/scripts")
    from ingest_survey import run
    run()


def _run_dq_checks():
    import sys
    sys.path.insert(0, "/opt/airflow/scripts")
    from dq_checks import run_checks
    run_checks()


default_args = {
    "owner": "swagat",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
    "email_on_retry": False,
}

with DAG(
    dag_id="stackoverflow_survey_pipeline",
    schedule_interval="@weekly",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["stackoverflow", "data-engineering", "survey"],
    default_args=default_args,
) as dag:
    ingest_raw_survey = PythonOperator(
        task_id="ingest_raw_survey",
        python_callable=_run_ingest,
    )

    run_dq_checks = PythonOperator(
        task_id="run_dq_checks",
        python_callable=_run_dq_checks,
    )

    dbt_run_models = BashOperator(
        task_id="dbt_run_models",
        bash_command="cd /opt/airflow/dbt_project && dbt run --profiles-dir /opt/airflow/dbt_project --target prod",
    )

    dbt_test_models = BashOperator(
        task_id="dbt_test_models",
        bash_command="cd /opt/airflow/dbt_project && dbt test --profiles-dir /opt/airflow/dbt_project --target prod",
    )

    ingest_raw_survey >> run_dq_checks >> dbt_run_models >> dbt_test_models
