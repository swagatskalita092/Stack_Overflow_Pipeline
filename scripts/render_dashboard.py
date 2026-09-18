"""Render a static dashboard from the currently published mart views.

Why this script exists
----------------------
There is no hosted warehouse for a public page to query live. After
publish_release, this script reads marts.v_* (the same views every other
reader uses), draws inline SVG from those numbers, and writes
docs/site/index.html. GitHub Pages serves that file. The page is as live
as the last successful publish, which is why it prints published_at.

This module never writes dwh.active_release or marts.mart_*. A crash here
is a presentation bug. The DAG runs it *after* publish_release for that
reason.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
_TEMPLATES = _SCRIPTS / "dashboard_templates"
_CONTRACTS = _ROOT / "docs" / "data_contracts.md"
DEFAULT_OUT_DIR = _ROOT / "docs" / "site"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from db import get_connection  # noqa: E402

logger = logging.getLogger(__name__)


def _num(value) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _fmt(value, decimals: int = 1) -> str:
    """Stable text so tests can grep the HTML for the queried figure."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        value = float(value)
    if decimals == 0:
        return str(int(round(value)))
    return f"{value:.{decimals}f}"


def _short_uuid(release_id: str) -> str:
    return str(release_id).split("-")[0]


def _contract_section(markdown: str, heading: str) -> str:
    """Return the body under `## {heading}` verbatim (no paraphrase)."""
    pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, markdown, flags=re.M | re.S)
    if not match:
        raise RuntimeError(f"heading {heading!r} missing from {_CONTRACTS}")
    return match.group(1).strip()


def load_contracts(path: Path = _CONTRACTS) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {
        "salary": _contract_section(text, "`mart_salary_analytics`"),
        "tech": _contract_section(text, "`mart_tech_adoption`"),
        "ai": _contract_section(text, "`mart_ai_sentiment`"),
        "currency": _contract_section(text, "Shared: what `comp_total_raw` is (and is not)"),
    }


def svg_bars(
    rows: list[dict[str, Any]],
    *,
    value_key: str,
    label_key: str,
    unit: str = "",
    decimals: int = 1,
    bar_id_prefix: str = "bar",
) -> str:
    """Horizontal bars. NULL values become a 'Not asked in <year>' badge, not a 0-width bar.

    The numeric text is in the SVG itself so tests can assert on the HTML
    without running JavaScript.
    """
    asked = [r for r in rows if r.get(value_key) is not None]
    missing = [r for r in rows if r.get(value_key) is None]
    chunks: list[str] = []
    if asked:
        max_v = max(float(r[value_key]) for r in asked) or 1.0
        height = 28 * len(asked) + 8
        width = 900
        label_w = 420
        bar_max = 360
        parts = [
            f'<svg class="chart" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="bar chart" data-series="{html.escape(bar_id_prefix)}">'
        ]
        for i, row in enumerate(asked):
            y = 4 + i * 28
            val = float(row[value_key])
            bar_w = 0 if max_v <= 0 else round(bar_max * (val / max_v), 2)
            label = html.escape(str(row[label_key]))
            shown = html.escape(_fmt(val, decimals) + unit)
            year = row.get("survey_year", "")
            parts.append(
                f'<g class="bar-row" data-asked="true" data-year="{year}" '
                f'data-metric="{html.escape(value_key)}" data-value="{shown}">'
                f'<text x="0" y="{y + 16}" class="bar-label">{label}</text>'
                f'<rect x="{label_w}" y="{y + 4}" width="{bar_w}" height="16" '
                f'class="bar" data-bar-value="{shown}"></rect>'
                f'<text x="{label_w + bar_w + 8}" y="{y + 16}" class="bar-num">{shown}</text>'
                f"</g>"
            )
        parts.append("</svg>")
        chunks.append("".join(parts))
    for row in missing:
        year = row.get("survey_year", "")
        label = html.escape(str(row[label_key]))
        chunks.append(
            f'<p class="not-asked" data-asked="false" data-year="{year}" '
            f'data-metric="{html.escape(value_key)}">'
            f'<span class="badge">Not asked in {html.escape(str(year))}</span> '
            f"{label}"
            f"</p>"
        )
    if not chunks:
        return '<p class="empty">No published rows for this chart.</p>'
    return "\n".join(chunks)


def fetch_published(conn) -> dict[str, Any]:
    """Read only v_* views plus dwh.active_release. Never marts.mart_*."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.survey_year,
                   a.release_id::text,
                   r.published_at
            FROM dwh.active_release a
            JOIN dwh.pipeline_releases r ON r.release_id = a.release_id
            ORDER BY a.survey_year
            """
        )
        active = cur.fetchall()

        cur.execute(
            """
            SELECT survey_year, country, experience_band, dev_type, remote_work,
                   org_size, respondent_count, median_salary, avg_salary
            FROM marts.v_salary_analytics
            ORDER BY survey_year, respondent_count DESC
            """
        )
        salary = cur.fetchall()

        cur.execute(
            """
            SELECT survey_year, tech_type, tech_name, total_users,
                   retention_rate_pct, usage_rank
            FROM marts.v_tech_adoption
            ORDER BY survey_year, tech_type, usage_rank, tech_name
            """
        )
        tech = cur.fetchall()

        cur.execute(
            """
            SELECT survey_year, country, dev_type, ai_select, ai_sent, ai_threat,
                   respondent_count, avg_job_satisfaction, pct_see_ai_as_threat
            FROM marts.v_ai_sentiment
            ORDER BY survey_year, respondent_count DESC
            """
        )
        ai = cur.fetchall()

    years = []
    for survey_year, release_id, published_at in active:
        years.append(
            {
                "survey_year": int(survey_year),
                "release_id": release_id,
                "release_id_short": _short_uuid(release_id),
                "published_at": published_at,
            }
        )

    salary_rows = []
    for row in salary:
        salary_rows.append(
            {
                "survey_year": int(row[0]),
                "country": row[1],
                "experience_band": row[2],
                "dev_type": row[3],
                "remote_work": row[4],
                "org_size": row[5],
                "respondent_count": int(row[6]) if row[6] is not None else 0,
                "median_salary": _num(row[7]),
                "avg_salary": _num(row[8]),
                "label": f"{row[1]} · {row[2]} · {row[3]}",
            }
        )

    tech_rows = []
    for row in tech:
        tech_rows.append(
            {
                "survey_year": int(row[0]),
                "tech_type": row[1],
                "tech_name": row[2],
                "total_users": int(row[3]) if row[3] is not None else 0,
                "retention_rate_pct": _num(row[4]),
                "usage_rank": int(row[5]) if row[5] is not None else None,
                "label": f"{row[2]} ({row[1]})",
            }
        )

    ai_rows = []
    for row in ai:
        ai_rows.append(
            {
                "survey_year": int(row[0]),
                "country": row[1],
                "dev_type": row[2],
                "ai_select": row[3],
                "ai_sent": row[4],
                "ai_threat": row[5],
                "respondent_count": int(row[6]) if row[6] is not None else 0,
                "avg_job_satisfaction": _num(row[7]),
                "pct_see_ai_as_threat": _num(row[8]),
                "label": f"{row[1]} · {row[2]} · {row[3] or '—'} / {row[4] or '—'}",
            }
        )

    return {
        "years": years,
        "salary": salary_rows,
        "tech": tech_rows,
        "ai": ai_rows,
    }


def _year_payload(meta: dict, salary, tech, ai) -> dict[str, Any]:
    year = meta["survey_year"]
    s = [r for r in salary if r["survey_year"] == year]
    t = [r for r in tech if r["survey_year"] == year]
    a = [r for r in ai if r["survey_year"] == year]
    langs = [r for r in t if r["tech_type"] == "Language"][:10]
    threat_chart_rows = [
        {**r, "survey_year": year} for r in a[:8]
    ]
    job_chart_rows = [
        {**r, "survey_year": year} for r in a[:8]
    ]
    return {
        **meta,
        "published_at_label": (
            meta["published_at"].isoformat(sep=" ", timespec="seconds")
            if meta["published_at"] is not None
            else "unknown"
        ),
        "salary_cell_respondents": sum(r["respondent_count"] for r in s),
        "ai_cell_respondents": sum(r["respondent_count"] for r in a),
        "tech_language_users_top": sum(r["total_users"] for r in langs),
        "salary_svg": svg_bars(
            s[:8],
            value_key="median_salary",
            label_key="label",
            unit="",
            decimals=2,
            bar_id_prefix=f"salary-{year}",
        ),
        "tech_svg": svg_bars(
            langs,
            value_key="total_users",
            label_key="label",
            unit="",
            decimals=0,
            bar_id_prefix=f"tech-{year}",
        ),
        "ai_threat_svg": svg_bars(
            threat_chart_rows,
            value_key="pct_see_ai_as_threat",
            label_key="label",
            unit="%",
            decimals=1,
            bar_id_prefix=f"threat-{year}",
        ),
        "ai_job_svg": svg_bars(
            job_chart_rows,
            value_key="avg_job_satisfaction",
            label_key="label",
            unit="",
            decimals=2,
            bar_id_prefix=f"jobsat-{year}",
        ),
        "salary_n_cells": len(s),
        "tech_n_cells": len(t),
        "ai_n_cells": len(a),
    }


def _yoy(years: list[dict], salary, tech, ai) -> dict[str, Any]:
    """Pairs across years. Disabled (not fabricated) when only one year is live."""
    year_nums = [y["survey_year"] for y in years]
    if len(year_nums) < 2:
        return {"enabled": False, "salary": [], "tech": [], "ai": []}

    def index(rows, keys):
        out = {}
        for r in rows:
            k = tuple(r[c] for c in keys)
            out.setdefault(k, {})[r["survey_year"]] = r
        return out

    salary_idx = index(
        salary,
        ("country", "experience_band", "dev_type", "remote_work", "org_size"),
    )
    tech_idx = index(tech, ("tech_type", "tech_name"))
    # Grain of the AI mart includes ai_threat, which is NULL for years that
    # did not ask the question. Pair on the answers that exist in both years
    # so a 2023 NULL can sit next to a 2024 rate instead of looking like a
    # different cell.
    ai_idx = index(ai, ("country", "dev_type", "ai_select", "ai_sent"))

    def pairs(idx, value_key, label_fn):
        found = []
        for key, by_year in idx.items():
            if len(by_year) < 2:
                continue
            found.append(
                {
                    "label": label_fn(key, by_year),
                    "by_year": {
                        y: {
                            "value": by_year[y].get(value_key),
                            "survey_year": y,
                            "label": f"{y}",
                        }
                        for y in sorted(by_year)
                    },
                }
            )
        return found[:12]

    salary_pairs = pairs(
        salary_idx,
        "median_salary",
        lambda k, _b: f"{k[0]} · {k[1]} · {k[2]}",
    )
    tech_pairs = pairs(
        tech_idx,
        "total_users",
        lambda k, _b: f"{k[1]} ({k[0]})",
    )
    ai_pairs = pairs(
        ai_idx,
        "pct_see_ai_as_threat",
        lambda k, _b: f"{k[0]} · {k[1]} · {k[2] or '—'} / {k[3] or '—'}",
    )

    def pair_svg(pair_list, value_key, unit, decimals, prefix):
        blocks = []
        for i, pair in enumerate(pair_list):
            blocks.append(
                {
                    "label": pair["label"],
                    "svg": svg_bars(
                        [
                            {
                                "survey_year": y,
                                "label": str(y),
                                value_key: info["value"],
                            }
                            for y, info in pair["by_year"].items()
                        ],
                        value_key=value_key,
                        label_key="label",
                        unit=unit,
                        decimals=decimals,
                        bar_id_prefix=f"{prefix}-{i}",
                    ),
                }
            )
        return blocks

    return {
        "enabled": True,
        "salary": pair_svg(salary_pairs, "median_salary", "", 2, "yoy-salary"),
        "tech": pair_svg(tech_pairs, "total_users", "", 0, "yoy-tech"),
        "ai": pair_svg(ai_pairs, "pct_see_ai_as_threat", "%", 1, "yoy-ai"),
        "salary_empty": not salary_pairs,
        "tech_empty": not tech_pairs,
        "ai_empty": not ai_pairs,
    }


def build_context(conn) -> dict[str, Any]:
    data = fetch_published(conn)
    year_pages = [
        _year_payload(meta, data["salary"], data["tech"], data["ai"])
        for meta in data["years"]
    ]
    as_of = None
    for y in year_pages:
        ts = y.get("published_at")
        if ts is not None and (as_of is None or ts > as_of):
            as_of = ts
    return {
        "years": year_pages,
        "year_list": [y["survey_year"] for y in year_pages],
        "data_as_of": (
            as_of.isoformat(sep=" ", timespec="seconds") if as_of else "nothing published"
        ),
        "nothing_published": len(year_pages) == 0,
        "yoy": _yoy(data["years"], data["salary"], data["tech"], data["ai"]),
        "contracts": load_contracts(),
    }


def render(out_dir: Optional[Path] = None) -> Path:
    """Query the warehouse and write docs/site/index.html (or out_dir)."""
    dest = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".nojekyll").write_text("", encoding="utf-8")
    # GitHub Pages "Deploy from /docs" looks here, not only in docs/site/.
    if dest.resolve() == DEFAULT_OUT_DIR.resolve():
        (DEFAULT_OUT_DIR.parent / ".nojekyll").write_text("", encoding="utf-8")

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("index.html.j2")

    conn = get_connection()
    try:
        ctx = build_context(conn)
    finally:
        conn.close()

    html_out = template.render(**ctx)
    target = dest / "index.html"
    target.write_text(html_out, encoding="utf-8")
    logger.info(
        "Wrote %s (years=%s data_as_of=%s)",
        target,
        ctx["year_list"],
        ctx["data_as_of"],
    )
    return target


def main(argv: Optional[list[str]] = None) -> Path:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Render docs/site from published marts.v_*")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: docs/site).",
    )
    args = parser.parse_args(argv)
    return render(out_dir=args.out_dir)


if __name__ == "__main__":
    main()
