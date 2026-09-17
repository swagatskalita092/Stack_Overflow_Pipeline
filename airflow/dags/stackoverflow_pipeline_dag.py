"""Weekly Stack Overflow survey pipeline: ingest → DQ → dbt → publish.

Why a DAG instead of a cron script
----------------------------------
The steps must stay in order, and publication is a separate step from
building. dbt writing mart rows is not the same as those rows becoming
what a reader sees. Only publish_release moves dwh.active_release, and
only after dbt_test_models succeeds.

A crash after dbt test (candidate_ready) and before publish leaves the
previous published release in the views. A failed dbt test never calls
publish_release; on_failure_callback marks the open release failed.
"""

from datetime import datetime, timedelta
import sys

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

sys.path.insert(0, "/opt/airflow/scripts")


def _open_release(**context):
    """Mint a release_id, insert pipeline_releases (building), push to XCom.

    uuid5(run_id) is stable across Airflow retries of this task so we do
    not orphan a building row and then dbt-append under a new id.
    """
    import uuid
    from release import open_release

    rid = str(uuid.uuid5(uuid.NAMESPACE_DNS, context["run_id"]))
    return open_release(release_id=rid)


def _run_ingest():
    """Download the survey ZIP and replace raw.survey_responses."""
    from ingest_survey import run
    run()


def _record_checksum(**context):
    """Hash the raw table now that ingest has written it.

    If this hash matches the currently published release, the candidate
    will be named candidate_ready_unchanged_source. We still build.
    """
    from release import record_source_checksum
    rid = context["ti"].xcom_pull(task_ids="open_release")
    record_source_checksum(rid)


def _run_dq_checks(**context):
    """Landing-table checks. Blocking failures raise PublicationBlocked."""
    from dq_checks import run_checks
    from release import attach_dq_summary

    rid = context["ti"].xcom_pull(task_ids="open_release")
    summary = run_checks()
    attach_dq_summary(rid, summary)
    return summary


def _mark_candidate(**context):
    """dbt tests passed. Flip building → candidate_ready. Do not publish yet."""
    from release import mark_candidate
    rid = context["ti"].xcom_pull(task_ids="open_release")
    return mark_candidate(rid)


def _publish_release(**context):
    """One locked transaction: active_release := this candidate."""
    from release import publish_release
    rid = context["ti"].xcom_pull(task_ids="open_release")
    return publish_release(rid)


def _on_release_failed(context):
    """Any task failure after open_release stamps that id as failed."""
    from release import on_release_failed
    on_release_failed(context)


default_args = {
    "owner": "swagat",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
    "email_on_retry": False,
    "on_failure_callback": _on_release_failed,
}

with DAG(
    dag_id="stackoverflow_survey_pipeline",
    schedule_interval="@weekly",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["stackoverflow", "data-engineering", "survey"],
    default_args=default_args,
) as dag:
    open_release = PythonOperator(
        task_id="open_release",
        python_callable=_open_release,
    )

    ingest_raw_survey = PythonOperator(
        task_id="ingest_raw_survey",
        python_callable=_run_ingest,
    )

    record_source_checksum = PythonOperator(
        task_id="record_source_checksum",
        python_callable=_record_checksum,
    )

    run_dq_checks = PythonOperator(
        task_id="run_dq_checks",
        python_callable=_run_dq_checks,
    )

    dbt_run_models = BashOperator(
        task_id="dbt_run_models",
        bash_command=(
            "cd /opt/airflow/dbt_project && "
            "dbt run --profiles-dir /opt/airflow/dbt_project --target prod "
            '--vars \'{"release_id": "{{ ti.xcom_pull(task_ids="open_release") }}"}\''
        ),
    )

    dbt_test_models = BashOperator(
        task_id="dbt_test_models",
        bash_command=(
            "cd /opt/airflow/dbt_project && "
            "dbt test --profiles-dir /opt/airflow/dbt_project --target prod "
            '--vars \'{"release_id": "{{ ti.xcom_pull(task_ids="open_release") }}"}\''
        ),
    )

    mark_candidate = PythonOperator(
        task_id="mark_candidate",
        python_callable=_mark_candidate,
    )

    publish_release = PythonOperator(
        task_id="publish_release",
        python_callable=_publish_release,
    )

    (
        open_release
        >> ingest_raw_survey
        >> record_source_checksum
        >> run_dq_checks
        >> dbt_run_models
        >> dbt_test_models
        >> mark_candidate
        >> publish_release
    )
