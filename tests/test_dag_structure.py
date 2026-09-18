"""DAG wiring check without importing Apache Airflow.

Why not DagBag
--------------
A real `airflow.models.DagBag` import would need apache-airflow installed in
CI. This repo pins Airflow **2.8.1** (see docker-compose.yml), which does not
support Python 3.12, and even on 3.11 the install is hundreds of MB. That
fights the reason CI uses a postgres service container instead of
docker-compose: keep the PR job fast, and never wait on extra services.

DagBag would not hit the survey CDN at parse time (the DAG only imports
ingest/release inside the task callables). The cost is the Airflow install
and a metadata DB / AIRFLOW_HOME, not a network call.

What we do instead: parse the DAG file with the stdlib `ast` module and read
the `>>` chain. That fails CI if someone moves `publish_release` before
`dbt_test_models`, which is the publication-safety regression this check is
for. No postgres, no CDN, no Airflow package.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = ROOT / "airflow" / "dags" / "stackoverflow_pipeline_dag.py"

# Variable names in the `>>` chain. They match the Airflow task_id values.
EXPECTED_CHAIN = [
    "open_release",
    "ingest_raw_survey",
    "record_source_checksum",
    "run_dq_checks",
    "dbt_run_models",
    "dbt_test_models",
    "mark_candidate",
    "publish_release",
    "render_dashboard",
]


def _chain_names(node: ast.AST) -> list[str]:
    """Flatten `a >> b >> c` into ['a', 'b', 'c']."""
    if isinstance(node, ast.Tuple) and len(node.elts) == 1:
        return _chain_names(node.elts[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift):
        return _chain_names(node.left) + _chain_names(node.right)
    if isinstance(node, ast.Name):
        return [node.id]
    return []


def _rshift_chains(tree: ast.AST) -> list[list[str]]:
    """Top-level `a >> b >> c` expressions (the DAG dependency line)."""
    chains: list[list[str]] = []
    for node in tree.body:
        if isinstance(node, ast.With):
            for item in node.body:
                if not isinstance(item, ast.Expr):
                    continue
                names = _chain_names(item.value)
                if len(names) >= 2:
                    chains.append(names)
    return chains


def _task_ids_from_keywords(tree: ast.AST) -> set[str]:
    """task_id="..." values on operator constructors."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "task_id" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    found.add(kw.value.value)
    return found


def test_dag_file_exists():
    assert DAG_PATH.is_file(), f"missing DAG file: {DAG_PATH}"


def test_dag_task_chain_is_ingest_then_test_then_publish():
    """The only `>>` chain must be the full pipeline in publication-safe order."""
    source = DAG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DAG_PATH))
    chains = _rshift_chains(tree)
    assert chains, "DAG file has no `>>` dependency chain"
    assert chains == [EXPECTED_CHAIN], (
        "DAG task order changed.\n"
        f"expected: {EXPECTED_CHAIN}\n"
        f"found:    {chains}"
    )


def test_dag_task_ids_cover_the_chain():
    source = DAG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DAG_PATH))
    task_ids = _task_ids_from_keywords(tree)
    missing = set(EXPECTED_CHAIN) - task_ids
    assert not missing, f"task_id missing for chained names: {missing}"


def _operator_kwargs_for_task(tree: ast.AST, task_id: str) -> dict[str, ast.AST]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        tid = kwargs.get("task_id")
        if isinstance(tid, ast.Constant) and tid.value == task_id:
            return kwargs
    raise AssertionError(f"no operator with task_id={task_id!r}")


def test_render_dashboard_overrides_failure_callback_so_publish_stays():
    """A chart crash must not call _on_release_failed / mark_failed.

    default_args still uses _on_release_failed for ingest/DQ/dbt. The
    render task sets its own callback. Combined with `publish_release >>
    render_dashboard`, a failed chart cannot unpublish.
    """
    source = DAG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DAG_PATH))
    kwargs = _operator_kwargs_for_task(tree, "render_dashboard")
    cb = kwargs.get("on_failure_callback")
    assert cb is not None, "render_dashboard must override on_failure_callback"
    assert isinstance(cb, ast.Name)
    assert cb.id == "_render_failed_leave_publish_alone"
    assert cb.id != "_on_release_failed"


def test_dag_module_does_not_import_ingest_at_parse_time():
    """Parse-time imports must not pull in the CDN client.

    ingest_survey is imported inside `_run_ingest`, so `from airflow import
    DAG` (or this AST check) cannot download the survey ZIP. A top-level
    `import ingest_survey` would make DagBag / `python dag.py` need the
    network: that is the PR-blocking failure this assertion guards.
    """
    source = DAG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DAG_PATH))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            dumped = ast.dump(node)
            assert "ingest_survey" not in dumped
            assert "requests" not in dumped
            assert "render_dashboard" not in dumped
