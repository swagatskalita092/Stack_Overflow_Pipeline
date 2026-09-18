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

survey_year is explicit. Pass {"survey_year": 2023} in the DAG run conf
(or set SURVEY_YEAR). Default is 2024. We do not guess the year from
the downloaded file.
"""

from datetime import datetime, timedelta
import os
import sys

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

sys.path.insert(0, "/opt/airflow/scripts")


def _survey_year_from_context(context) -> int:
    """dag_run.conf.survey_year, else SURVEY_YEAR, else 2024."""
    from ingest_survey import resolve_survey_year

    conf = {}
    dag_run = context.get("dag_run")
    if dag_run is not None and getattr(dag_run, "conf", None):
        conf = dag_run.conf or {}
    explicit = conf.get("survey_year", os.getenv("SURVEY_YEAR"))
    return resolve_survey_year(explicit)


def _open_release(**context):
    """Mint a release_id, insert pipeline_releases (building), push to XCom.

    uuid5(run_id) is stable across Airflow retries of this task so we do
    not orphan a building row and then dbt-append under a new id.
    survey_year is pushed as a second XCom key for ingest / DQ / dbt.
    """
    import uuid
    from release import open_release

    year = _survey_year_from_context(context)
    rid = str(uuid.uuid5(uuid.NAMESPACE_DNS, context["run_id"]))
    context["ti"].xcom_push(key="survey_year", value=year)
    return open_release(release_id=rid, survey_year=year)


def _run_ingest(**context):
    """Download the survey extract and replace that year's raw rows."""
    from ingest_survey import run

    year = context["ti"].xcom_pull(task_ids="open_release", key="survey_year")
    run(survey_year=year)


def _record_checksum(**context):
    """Hash this year's raw rows now that ingest has written them.

    If this hash matches the currently published release *for this year*,
    the candidate will be named candidate_ready_unchanged_source. We still
    build.
    """
    from release import record_source_checksum
    rid = context["ti"].xcom_pull(task_ids="open_release")
    record_source_checksum(rid)


def _run_dq_checks(**context):
    """Landing-table checks for this year. Blocking failures raise PublicationBlocked."""
    from dq_checks import run_checks
    from release import attach_dq_summary

    rid = context["ti"].xcom_pull(task_ids="open_release")
    year = context["ti"].xcom_pull(task_ids="open_release", key="survey_year")
    summary = run_checks(survey_year=year)
    attach_dq_summary(rid, summary)
    return summary


def _mark_candidate(**context):
    """dbt tests passed. Flip building → candidate_ready. Do not publish yet."""
    from release import mark_candidate
    rid = context["ti"].xcom_pull(task_ids="open_release")
    return mark_candidate(rid)


def _publish_release(**context):
    """One locked transaction: this year's active_release := this candidate."""
    from release import publish_release
    rid = context["ti"].xcom_pull(task_ids="open_release")
    return publish_release(rid)


def _render_dashboard(**context):
    """Write docs/site from marts.v_*. Runs after publish; must not unpublish.

    Import is inside the callable so DagBag parse does not need Jinja2/Postgres.
    """
    from render_dashboard import render

    return str(render())


def _on_release_failed(context):
    """Any task failure after open_release stamps that id as failed.

    Not used by render_dashboard: that task overrides on_failure_callback so
    a chart crash cannot call mark_failed.
    """
    from release import on_release_failed
    on_release_failed(context)


def _render_failed_leave_publish_alone(context):
    """Presentation failure after a successful publish. Pointer stays put."""
    import logging

    logging.getLogger("airflow.task").warning(
        "render_dashboard failed; leaving dwh.active_release untouched "
        "(publish_release already committed)"
    )


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
            '--vars \'{"release_id": "{{ ti.xcom_pull(task_ids="open_release") }}", '
            '"survey_year": {{ ti.xcom_pull(task_ids="open_release", key="survey_year") | default(2024) }}}}\''
        ),
    )

    dbt_test_models = BashOperator(
        task_id="dbt_test_models",
        bash_command=(
            "cd /opt/airflow/dbt_project && "
            "dbt test --profiles-dir /opt/airflow/dbt_project --target prod "
            '--vars \'{"release_id": "{{ ti.xcom_pull(task_ids="open_release") }}", '
            '"survey_year": {{ ti.xcom_pull(task_ids="open_release", key="survey_year") | default(2024) }}}}\''
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

    render_dashboard = PythonOperator(
        task_id="render_dashboard",
        python_callable=_render_dashboard,
        # Override default_args: a broken chart must not mark_failed.
        on_failure_callback=_render_failed_leave_publish_alone,
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
        >> render_dashboard
    )
