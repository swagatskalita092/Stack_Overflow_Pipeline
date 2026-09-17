"""Weekly Stack Overflow survey pipeline: ingest → DQ → dbt run → dbt test.

Why a DAG instead of a cron script
----------------------------------
The four steps must stay in order. Ingest without DQ would still let dbt
publish. dbt test without dbt run would test last week's tables. Airflow
retries a failed task without re-running the ones that already succeeded.

Scripts and the dbt project are mounted at /opt/airflow/ inside Compose.
The PythonOperators add /opt/airflow/scripts to sys.path so they can import
ingest_survey and dq_checks the same way a local `python scripts/...` would.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator


def _run_ingest():
    """Task 1: download the survey ZIP and replace raw.survey_responses.

    Import is inside the function so the DAG file can parse on a scheduler
    that does not have the survey scripts on PYTHONPATH until the task runs.
    """
    import sys
    sys.path.insert(0, "/opt/airflow/scripts")
    from ingest_survey import run
    run()


def _run_dq_checks():
    """Task 2: six landing-table checks into dwh.dq_issues.

    Today this always succeeds even when checks fire (policy is log-only).
    docs/data_quality_policy.md is the Phase B contract for when this task
    should fail and stop dbt from publishing.
    """
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

    # profiles-dir is the project folder so Compose does not need ~/.dbt.
    dbt_run_models = BashOperator(
        task_id="dbt_run_models",
        bash_command="cd /opt/airflow/dbt_project && dbt run --profiles-dir /opt/airflow/dbt_project --target prod",
    )

    dbt_test_models = BashOperator(
        task_id="dbt_test_models",
        bash_command="cd /opt/airflow/dbt_project && dbt test --profiles-dir /opt/airflow/dbt_project --target prod",
    )

    ingest_raw_survey >> run_dq_checks >> dbt_run_models >> dbt_test_models
