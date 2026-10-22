"""Pure tests of the story dashboard: no Postgres, no warehouse."""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path

from dq_checks import _dq_checks
from render_dashboard import (
    build_story_context,
    currency_wording,
    render_html,
    story_databases,
    story_jobsat,
    story_languages,
    story_respondents,
    story_salaries,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "dashboard_templates" / "index.html.j2"
DAG_PATH = ROOT / "airflow" / "dags" / "stackoverflow_pipeline_dag.py"
STATIC_PAGE = ROOT / "docs" / "site" / "index.html"


def _lang(year, name, users, rank):
    return {
        "survey_year": year,
        "tech_type": "Language",
        "tech_name": name,
        "total_users": users,
        "usage_rank": rank,
    }


def _db(year, name, users, rank):
    return {
        "survey_year": year,
        "tech_type": "Database",
        "tech_name": name,
        "total_users": users,
        "usage_rank": rank,
    }


def _salary(year, country, band, role, remote, org, median, n=10):
    return {
        "survey_year": year,
        "country": country,
        "experience_band": band,
        "dev_type": role,
        "remote_work": remote,
        "org_size": org,
        "respondent_count": n,
        "median_salary": median,
    }


def _ai(
    year,
    country,
    role,
    select,
    sent,
    score,
    n=10,
    threat=None,
    pct=None,
):
    return {
        "survey_year": year,
        "country": country,
        "dev_type": role,
        "ai_select": select,
        "ai_sent": sent,
        "ai_threat": threat,
        "respondent_count": n,
        "avg_job_satisfaction": score,
        "pct_see_ai_as_threat": pct,
    }


def test_language_order_identical():
    names = ["JavaScript", "Python", "SQL"]
    tech = []
    for rank, name in enumerate(names, start=1):
        tech.append(_lang(2023, name, 100 - rank, rank))
        tech.append(_lang(2024, name, 80 - rank, rank))
    out = story_languages(tech, [2023, 2024])
    assert "JavaScript tops both years" in out["lead_sentence"]
    assert "exactly the same order" in out["order_sentence"]


def test_language_order_changed():
    tech = [
        _lang(2023, "JavaScript", 100, 1),
        _lang(2023, "Python", 90, 2),
        _lang(2024, "Python", 80, 1),
        _lang(2024, "JavaScript", 70, 2),
    ]
    out = story_languages(tech, [2023, 2024])
    assert "The order changed:" in out["order_sentence"]
    assert "rank 1" in out["order_sentence"]


def test_databases_all_lower():
    tech = [
        _db(2023, "PostgreSQL", 100, 1),
        _db(2023, "MySQL", 80, 2),
        _db(2024, "PostgreSQL", 70, 1),
        _db(2024, "MySQL", 50, 2),
    ]
    out = story_databases(tech, [2023, 2024])
    assert "PostgreSQL leads in both years" in out["lead_sentence"]
    assert "Every database on this list has a lower count in 2024" in out[
        "change_sentence"
    ]


def test_databases_not_all_lower():
    tech = [
        _db(2023, "PostgreSQL", 100, 1),
        _db(2023, "MySQL", 80, 2),
        _db(2024, "PostgreSQL", 70, 1),
        _db(2024, "MySQL", 90, 2),
    ]
    out = story_databases(tech, [2023, 2024])
    assert "1 of 2 databases have a lower count in 2024" in out["change_sentence"]


def test_dynamodb_display_name():
    tech = [
        _db(2023, "Dynamodb", 100, 1),
        _db(2024, "Dynamodb", 80, 1),
    ]
    out = story_databases(tech, [2023, 2024])
    assert out["rows"][0]["name"] == "DynamoDB"
    assert "(Database)" not in out["rows"][0]["name"]


def test_salary_lower_higher_and_equal():
    rows = [
        _salary(2023, "United States of America", "10-19 years", "Developer, full-stack", "Remote", "A", 100),
        _salary(2024, "United States of America", "10-19 years", "Developer, full-stack", "Remote", "A", 80),
        _salary(2023, "United States of America", "5-9 years", "Developer, full-stack", "Remote", "A", 90),
        _salary(2024, "United States of America", "5-9 years", "Developer, full-stack", "Remote", "A", 110),
        _salary(2023, "United States of America", "20+ years", "Developer, full-stack", "Remote", "A", 95),
        _salary(2024, "United States of America", "20+ years", "Developer, full-stack", "Remote", "A", 95),
    ]
    out = story_salaries(rows, [2023, 2024])
    assert out["lower"] == 1
    assert out["higher"] == 1
    assert out["unchanged"] == 1
    assert "unchanged" in out["change_sentence"]
    assert "80" in out["range_sentence"] or "80" in format_join(out)


def format_join(out):
    return " ".join(
        [out.get("range_sentence") or "", out.get("change_sentence") or ""]
    )


def test_salary_single_year_does_not_require_pairs():
    rows = [
        _salary(
            2024,
            "United States of America",
            "5-9 years",
            "Developer, full-stack",
            "Remote",
            "A",
            125000,
        )
    ]
    out = story_salaries(rows, [2024])
    assert out["paired"] is False
    assert out["groups"][0]["bars"][0]["display"] == "125,000"
    assert out["change_sentence"] == ""
    assert "expected to be US dollars" in out["intro"]


def test_currency_wording_us_vs_other():
    assert "expected to be US dollars" in currency_wording("United States of America")
    assert "expected to be US dollars" not in currency_wording("Germany")
    assert currency_wording("Germany") == "in the currency people reported"


def test_jobsat_duplicate_label_appends_threat():
    rows = [
        _ai(2024, "United States", "Developer, full-stack", "Yes", "Favorable", 7.1, n=20, threat="Yes"),
        _ai(2024, "United States", "Developer, full-stack", "Yes", "Favorable", 7.0, n=19, threat="No"),
    ]
    out = story_jobsat(rows, [2024])
    assert len(out["rows"]) == 2
    assert out["rows"][0]["main"] == out["rows"][1]["main"]
    assert "answered the AI-threat question: Yes" in out["rows"][0]["sub"]
    assert "answered the AI-threat question: No" in out["rows"][1]["sub"]


def test_jobsat_contrast_only_when_both_exist():
    yes_only = [
        _ai(2024, "United States", "Developer, full-stack", "Yes", "Favorable", 7.1),
    ]
    assert story_jobsat(yes_only, [2024])["contrast_sentences"] == []

    both = yes_only + [
        _ai(
            2024,
            "United States",
            "Developer, full-stack",
            "No, and I don't plan to",
            None,
            6.9,
            n=8,
        )
    ]
    sentences = story_jobsat(both, [2024])["contrast_sentences"]
    assert len(sentences) == 1
    assert "who use AI tools" in sentences[0]
    assert "against" in sentences[0]


def test_missing_respondents_tile():
    years = [
        {"survey_year": 2023, "respondents": 89184, "published_at": datetime(2026, 9, 18)},
        {"survey_year": 2024, "respondents": None, "published_at": datetime(2026, 9, 18)},
    ]
    out = story_respondents(years)
    assert [t["year"] for t in out["tiles"]] == [2023]
    assert out["fewer_later_note"] is None


def test_fewer_later_note_only_when_smaller():
    smaller = story_respondents(
        [
            {"survey_year": 2023, "respondents": 100},
            {"survey_year": 2024, "respondents": 80},
        ]
    )
    assert "fewer people answered in 2024" in smaller["fewer_later_note"]
    bigger = story_respondents(
        [
            {"survey_year": 2023, "respondents": 80},
            {"survey_year": 2024, "respondents": 100},
        ]
    )
    assert bigger["fewer_later_note"] is None


def test_single_year_does_not_invent_second():
    data = {
        "years": [
            {
                "survey_year": 2024,
                "release_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "published_at": datetime(2026, 9, 18),
                "respondents": 65437,
            }
        ],
        "salary": [
            _salary(2024, "United States", "5-9 years", "Developer, full-stack", "Remote", "A", 125000)
        ],
        "tech": [_lang(2024, "Python", 10, 1)],
        "ai": [_ai(2024, "United States", "Developer, full-stack", "Yes", "Favorable", 8.0)],
    }
    ctx = build_story_context(data)
    html = render_html(ctx)
    assert 'data-published-years="2024"' in html
    assert 'data-yoy="false"' in html
    assert "does not invent a second year" in html
    assert "2023" not in html.split("Chapter 6")[0]
    assert "<script" not in html.lower()
    assert 'src="https://' not in html.lower()


def test_nothing_published():
    ctx = build_story_context({"years": [], "salary": [], "tech": [], "ai": []})
    html = render_html(ctx)
    assert "Nothing is published yet" in html
    assert 'data-yoy="false"' in html
    assert "Chapter 1" not in html


def test_pipeline_and_check_names_match_dag_and_dq():
    template = TEMPLATE.read_text(encoding="utf-8")
    dag_tree = ast.parse(DAG_PATH.read_text(encoding="utf-8"))
    dag_ids = set()
    for node in ast.walk(dag_tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "task_id" and isinstance(kw.value, ast.Constant):
                dag_ids.add(kw.value.value)
    chapter6 = template.split('id="c6"')[1].split('id="gl"')[0]
    ol = re.search(r"<ol>(.*?)</ol>", chapter6, re.S).group(1)
    task_names = re.findall(r"<li><code>([a-z_]+)</code>", ol)
    assert task_names == [
        "open_release",
        "ingest_raw_survey",
        "record_source_checksum",
        "run_dq_checks",
        "dbt_seed",
        "dbt_run_models",
        "dbt_test_models",
        "mark_candidate",
        "publish_release",
        "render_dashboard",
    ]
    assert set(task_names) == dag_ids

    check_names = re.findall(
        r"<li><code>([a-z_]+)</code>",
        template.split("Seven checks")[1].split("The key idea")[0],
    )
    from_code = [c["name"] for c in _dq_checks(2024)]
    assert check_names == from_code


def test_four_stop_three_log_matches_dq_flags():
    """Blocking vs log-only counts come from dq_checks.blocks_when, not the policy table."""
    checks = _dq_checks(2024)
    stop = [
        c["name"]
        for c in checks
        if c["blocks_when"](0) or c["blocks_when"](1)
    ]
    log_only = [
        c["name"]
        for c in checks
        if not c["blocks_when"](0) and not c["blocks_when"](1)
    ]
    assert len(stop) == 4
    assert len(log_only) == 3
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "Four stop the run when they fail. Three only write a note and carry on:" in template


def _golden_data():
    langs = [
        "JavaScript",
        "HTML/CSS",
        "Python",
        "SQL",
        "TypeScript",
        "Bash/Shell",
        "Java",
        "C#",
        "C++",
        "C",
    ]
    lang_2023 = [55711, 46396, 43158, 42623, 34041, 28351, 26757, 24193, 19634, 16940]
    lang_2024 = [37492, 31816, 30719, 30682, 23150, 20412, 18239, 16318, 13827, 12184]
    tech = []
    for rank, name in enumerate(langs, start=1):
        tech.append(_lang(2023, name, lang_2023[rank - 1], rank))
        tech.append(_lang(2024, name, lang_2024[rank - 1], rank))

    dbs = [
        ("PostgreSQL", 34909, 25536),
        ("MySQL", 31489, 21099),
        ("SQLite", 23678, 17365),
        ("MongoDB", 19556, 13007),
        ("Microsoft SQL Server", 19506, 13275),
        ("Redis", 15639, 10463),
        ("MariaDB", 13495, 8991),
        ("Elasticsearch", 10263, 6533),
        ("Oracle", 7507, 5273),
        ("Dynamodb", 6798, 4138),
        ("Firebase Realtime Database", 4939, 3018),
        ("Cloud Firestore", 4901, 2842),
    ]
    for rank, (name, a, b) in enumerate(dbs, start=1):
        tech.append(_db(2023, name, a, rank))
        tech.append(_db(2024, name, b, rank))

    salary_pairs = [
        (164000, 160000, "Remote", "A"),
        (155000, 152000, "Hybrid", "A"),
        (143500, 130000, "Remote", "B"),
        (140000, 145000, "Hybrid", "B"),
        (160000, 163000, "Office", "A"),
        (155000, 165000, "Remote", "C"),
        (150000, 139000, "Hybrid", "C"),
        (185000, 175000, "Remote", "D", "Developer, back-end"),
        (160000, 161500, "Office", "C"),
        (202500, 170000, "Remote", "E"),
        (162000, 152000, "Hybrid", "D", "Developer, back-end"),
        (150000, 160000, "Office", "E"),
    ]
    salary = []
    for i, item in enumerate(salary_pairs):
        a, b, remote, org = item[0], item[1], item[2], item[3]
        role = item[4] if len(item) > 4 else "Developer, full-stack"
        band = "10-19 years"
        if i in (2, 3, 6, 10):
            band = "5-9 years"
        if i in (5, 8):
            band = "20+ years"
        salary.append(
            _salary(2023, "United States of America", band, role, remote, org, a)
        )
        salary.append(
            _salary(2024, "United States of America", band, role, remote, org, b)
        )

    jobsat = [
        ("United States of America", "Developer, full-stack", "No, and I don't plan to", None, 6.94, 80),
        ("United States of America", "Developer, full-stack", "Yes", "Favorable", 7.13, 70),
        ("United States of America", "Developer, full-stack", "Yes", "Very favorable", 7.46, 60),
        ("United States of America", "Developer, back-end", "No, and I don't plan to", None, 7.02, 50),
        ("United States of America", "Developer, back-end", "Yes", "Favorable", 7.31, 40),
        ("United States of America", "Student", "No, and I don't plan to", None, 7.25, 30),
        ("Germany", "Developer, full-stack", "No, and I don't plan to", None, 6.74, 20),
        ("United Kingdom of Great Britain and Northern Ireland", "Developer, full-stack", "No, and I don't plan to", None, 7.02, 10),
    ]
    ai = []
    for country, role, select, sent, score, n in jobsat:
        ai.append(_ai(2024, country, role, select, sent, score, n=n, pct=None))
        ai.append(
            _ai(
                2023,
                country,
                role,
                select,
                sent,
                None,
                n=n,
                threat=None,
                pct=None,
            )
        )

    return {
        "years": [
            {
                "survey_year": 2023,
                "release_id": "a7160064-0000-0000-0000-000000000000",
                "published_at": datetime(2026, 9, 18, 12, 2, 26),
                "respondents": 89184,
            },
            {
                "survey_year": 2024,
                "release_id": "747c3d38-0000-0000-0000-000000000000",
                "published_at": datetime(2026, 9, 18, 12, 2, 26),
                "respondents": 65437,
            },
        ],
        "salary": salary,
        "tech": tech,
        "ai": ai,
    }


def test_golden_story_contains_static_page_numbers():
    html = render_html(build_story_context(_golden_data()))
    static = STATIC_PAGE.read_text(encoding="utf-8")
    numbers = re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2}", static)
    missing = [n for n in numbers if n not in html]
    assert missing == [], missing
    assert "Chapter 1" in html
    assert "Chapter 6" in html
    assert 'href="#c1">1 Languages' in html
    assert "Glossary" in html
    assert "Four stop the run when they fail. Three only write a note and carry on:" in html
    assert "DynamoDB" in html
    assert "<script" not in html.lower()
    assert "pct_see_ai_as_threat" not in html
    assert ">0.0%</span>" not in html
