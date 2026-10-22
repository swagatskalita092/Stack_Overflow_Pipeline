"""Dashboard renderer: real numbers in HTML, NULL ≠ zero bar, publish isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FIXTURES,
    ROOT,
    active_id,
    build_good_release,
    release_status,
)
from release import mark_failed, publish_release
from render_dashboard import render


def _html(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_render_includes_fixture_numbers(warehouse, tmp_path):
    """Phase A 2024 cell medians must appear as text in the generated HTML."""
    conn = warehouse
    rid = build_good_release(conn, survey_year=2024)
    assert publish_release(rid) == "published"

    out = render(out_dir=tmp_path)
    html = _html(out)
    assert "125,000" in html
    assert 'data-salary-respondents="6"' in html
    assert "United States" in html
    assert "8.00" in html
    assert "0.0%" not in html
    assert "pct_see_ai_as_threat" not in html
    assert rid.split("-")[0] in html
    assert 'data-published-years="2024"' in html
    assert "<script" not in html.lower()
    assert 'src="https://' not in html.lower()
    snippet = "is **self-reported survey compensation**"
    contracts = (ROOT / "docs" / "data_contracts.md").read_text(encoding="utf-8")
    assert snippet in contracts
    assert snippet not in html


def test_null_ai_metrics_get_not_asked_badge_not_zero_bar(warehouse, tmp_path):
    """2023 all-NULL threat/job_sat: the story says the questions were not asked."""
    conn = warehouse
    y24 = build_good_release(conn, survey_year=2024)
    publish_release(y24)
    y23 = build_good_release(
        conn,
        survey_year=2023,
        salary_path=FIXTURES / "expected_mart_salary_analytics_2023.csv",
        ai_path=FIXTURES / "expected_mart_ai_sentiment_2023.csv",
    )
    publish_release(y23)

    html = _html(render(out_dir=tmp_path))
    assert "The 2023 survey did not ask whether AI is a threat" in html
    assert "job-satisfaction" in html or "satisfied people are with their job" in html
    assert "0.0%" not in html
    assert "pct_see_ai_as_threat" not in html
    assert 'data-asked="false"' not in html
    assert "105,000" in html
    assert "Canada" in html
    assert 'data-yoy="true"' in html


def test_single_published_year_does_not_invent_yoy(warehouse, tmp_path):
    """Only 2024 published: page renders; YoY section does not fabricate 2023."""
    conn = warehouse
    rid = build_good_release(conn, survey_year=2024)
    publish_release(rid)

    html = _html(render(out_dir=tmp_path))
    assert 'data-published-years="2024"' in html
    assert 'data-provenance-year="2024"' in html
    assert 'data-provenance-year="2023"' not in html
    assert 'data-yoy="false"' in html
    assert "does not invent a second year" in html
    assert "125,000" in html


def test_render_failure_does_not_touch_published_release(warehouse, tmp_path, monkeypatch):
    """A chart crash after publish must leave dwh.active_release on that id.

    Mirrors the DAG: publish_release commits, then render runs. The DAG puts
    render after publish and gives it a no-op on_failure_callback. This test
    is the warehouse half: raise inside render, then mark_failed as the
    default callback would, and confirm the pointer and status did not move.
    """
    conn = warehouse
    rid = build_good_release(conn, survey_year=2024)
    assert publish_release(rid) == "published"
    live = active_id(conn, 2024)
    assert live == rid
    assert release_status(conn, rid) == "published"

    def boom(*_args, **_kwargs):
        raise RuntimeError("intentional dashboard render failure")

    monkeypatch.setattr("render_dashboard.build_context", boom)
    with pytest.raises(RuntimeError, match="intentional dashboard render failure"):
        render(out_dir=tmp_path)

    assert active_id(conn, 2024) == live
    assert release_status(conn, rid) == "published"

    # Default DAG callback calls mark_failed. That UPDATE skips published rows.
    mark_failed(rid, notes="render_dashboard failed")
    assert release_status(conn, rid) == "published"
    assert active_id(conn, 2024) == live
    assert not (tmp_path / "index.html").exists()


def test_renderer_sql_uses_views_not_mart_tables():
    src = (ROOT / "scripts" / "render_dashboard.py").read_text(encoding="utf-8")
    assert "FROM marts.v_salary_analytics" in src
    assert "FROM marts.v_tech_adoption" in src
    assert "FROM marts.v_ai_sentiment" in src
    assert "FROM marts.mart_" not in src
    assert "marts.mart_salary_analytics" not in src
    assert "marts.mart_tech_adoption" not in src
    assert "marts.mart_ai_sentiment" not in src
