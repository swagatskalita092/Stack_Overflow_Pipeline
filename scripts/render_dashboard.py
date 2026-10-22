"""Render a static story dashboard from the currently published mart views.

Why this script exists
----------------------
There is no hosted warehouse for a public page to query live. After
publish_release, this script reads marts.v_* (the same views every other
reader uses), builds the plain-language story page, and writes
docs/site/index.html. GitHub Pages serves that file. The page is as live
as the last successful publish, which is why it prints published_at.

This module never writes dwh.active_release or marts.mart_*. A crash here
is a presentation bug. The DAG runs it after publish_release for that
reason. Narrative claims (leads, same order, lower, higher) are computed
from the rows with a fallback when the claim is false or data is missing.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
from datetime import date, datetime
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

TECH_DISPLAY_NAMES = {
    "Dynamodb": "DynamoDB",
}

COUNTRY_DISPLAY_NAMES = {
    "United States of America": "United States",
    "United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
}

AI_SELECT_LABELS = {
    "Yes": "Uses AI tools",
    "No, and I don't plan to": "Does not use AI tools, no plan to",
    "No, but I plan to soon": "Does not use AI tools yet, plans to",
}

US_COUNTRY_NAMES = {
    "United States of America",
    "United States",
}


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


def format_count(value) -> str:
    """Integer with thousands separators (55,711)."""
    if value is None:
        return ""
    number = float(value)
    return f"{int(round(number)):,}"


def format_money(value) -> str:
    """Median with thousands separators. Keep a decimal only when needed."""
    if value is None:
        return ""
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}"


def format_score(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}"


def year_bar_class(year: int) -> str:
    return f"y{int(year) % 100:02d}"


def display_tech_name(name: str) -> str:
    raw = str(name or "")
    cleaned = re.sub(r"\s*\((Language|Database)\)\s*$", "", raw).strip()
    return TECH_DISPLAY_NAMES.get(cleaned, cleaned)


def display_country(name: Optional[str]) -> str:
    if not name:
        return ""
    return COUNTRY_DISPLAY_NAMES.get(name, name)


def display_role(dev_type: Optional[str]) -> str:
    if not dev_type:
        return ""
    text = str(dev_type)
    if text.lower().startswith("developer, "):
        return text.split(",", 1)[1].strip()
    return text


def is_united_states(country: Optional[str]) -> bool:
    return str(country or "") in US_COUNTRY_NAMES


def currency_wording(country: Optional[str]) -> str:
    """Amounts are always reported currency. USD expectation is US-only."""
    base = "in the currency people reported"
    if is_united_states(country):
        return f"{base}, which for the United States is expected to be US dollars"
    return base


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
        "currency": _contract_section(
            text, "Shared: what `comp_total_raw` is (and is not)"
        ),
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

    Kept for callers and tests that import this helper. The story page does
    not use it for the AI-threat rate, which is an artifact of the mart grain.
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


def bar_width_pct(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 0.0
    return round(100.0 * float(value) / float(max_value), 1)


def index_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict:
    """First-seen key order, then values keyed by survey_year."""
    out: dict = {}
    for row in rows:
        key = tuple(row.get(col) for col in keys)
        out.setdefault(key, {})[row["survey_year"]] = row
    return out


def overlapping_pairs(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...],
    *,
    cap: int = 12,
) -> list[dict[str, Any]]:
    """Groups that exist in more than one published year, first-seen order."""
    found = []
    for key, by_year in index_rows(rows, keys).items():
        if len(by_year) < 2:
            continue
        found.append({"key": key, "by_year": by_year})
        if len(found) >= cap:
            break
    return found


def _sorted_years(items: list[dict[str, Any]]) -> list[int]:
    years = []
    for item in items:
        year = item.get("survey_year")
        if year is not None:
            years.append(int(year))
    return sorted(set(years))


def story_respondents(years: list[dict[str, Any]]) -> dict[str, Any]:
    """Tiles from pipeline_releases.dq_summary total_rows_loaded."""
    tiles = []
    for meta in years:
        count = meta.get("respondents")
        if count is None:
            continue
        year = int(meta["survey_year"])
        tiles.append(
            {
                "year": year,
                "count": int(count),
                "display": format_count(count),
            }
        )
    note = None
    if len(tiles) >= 2 and tiles[-1]["count"] < tiles[0]["count"]:
        later = tiles[-1]["year"]
        note = (
            f"Keep this in mind: fewer people answered in {later}, so many "
            "counts below are smaller. That is not a sign that developers left. "
            "Compare the order of the lists and the gaps between rows, not the "
            "raw size across years."
        )
    return {"tiles": tiles, "fewer_later_note": note}


def story_languages(
    tech: list[dict[str, Any]], years: list[int]
) -> dict[str, Any]:
    langs = [r for r in tech if r.get("tech_type") == "Language"]
    if not langs or not years:
        return {
            "rows": [],
            "lead_sentence": "No published language rows passed the 100-user cutoff.",
            "order_sentence": "",
            "show_key": False,
        }

    by_year: dict[int, list[str]] = {}
    users: dict[tuple[str, int], int] = {}
    for year in years:
        ranked = sorted(
            [r for r in langs if r["survey_year"] == year],
            key=lambda r: (r.get("usage_rank") is None, r.get("usage_rank") or 0),
        )[:10]
        by_year[year] = [r["tech_name"] for r in ranked]
        for row in ranked:
            users[(row["tech_name"], year)] = int(row["total_users"])

    display_names = by_year[years[0]]
    if len(years) == 1:
        # Single year: that year's top 10, one bar each.
        display_names = by_year[years[0]]
    else:
        # Two or more years: earliest year's top 10, with later-year bars
        # when that language is present.
        display_names = by_year[years[0]]

    max_value = max(users.values()) if users else 1
    rows = []
    for name in display_names:
        bars = []
        for year in years:
            if (name, year) not in users:
                continue
            value = users[(name, year)]
            bars.append(
                {
                    "year": year,
                    "value": value,
                    "display": format_count(value),
                    "width_pct": bar_width_pct(value, max_value),
                    "css": year_bar_class(year),
                }
            )
        rows.append({"name": display_tech_name(name), "raw_name": name, "bars": bars})

    leaders = []
    for year in years:
        names = by_year.get(year) or []
        if names:
            leaders.append((year, display_tech_name(names[0])))
    if not leaders:
        lead_sentence = "No language ranked first in a published year."
    elif len(set(name for _, name in leaders)) == 1:
        name = leaders[0][1]
        if len(leaders) == 1:
            lead_sentence = f"{name} tops {leaders[0][0]}."
        else:
            lead_sentence = f"{name} tops both years."
    else:
        parts = [f"{name} tops {year}" for year, name in leaders]
        lead_sentence = ", ".join(parts) + "."

    order_sentence = ""
    if len(years) >= 2:
        lists = [by_year.get(year) or [] for year in years]
        if all(item == lists[0] for item in lists[1:]) and lists[0]:
            order_sentence = (
                "The top ten is in exactly the same order in every published year."
            )
        else:
            changed = "the lists differ"
            max_len = max((len(item) for item in lists), default=0)
            for rank in range(max_len):
                names_at_rank = []
                for item in lists:
                    names_at_rank.append(item[rank] if rank < len(item) else None)
                if len(set(names_at_rank)) > 1:
                    bits = []
                    for year, item in zip(years, lists):
                        label = (
                            display_tech_name(item[rank])
                            if rank < len(item)
                            else "missing"
                        )
                        bits.append(f"{year} is {label}")
                    changed = f"rank {rank + 1} " + ", ".join(bits)
                    break
            order_sentence = f"The order changed: {changed}."

    return {
        "rows": rows,
        "lead_sentence": lead_sentence,
        "order_sentence": order_sentence,
        "show_key": len(years) >= 1,
    }


def story_databases(
    tech: list[dict[str, Any]], years: list[int]
) -> dict[str, Any]:
    dbs = [r for r in tech if r.get("tech_type") == "Database"]
    if len(years) >= 2:
        pairs = overlapping_pairs(dbs, ("tech_type", "tech_name"), cap=12)
        source_rows = []
        for pair in pairs:
            name = pair["key"][1]
            for year, row in pair["by_year"].items():
                source_rows.append(
                    {
                        "name": name,
                        "year": year,
                        "value": int(row["total_users"]),
                    }
                )
        intro = (
            "Same idea, for databases. Only databases with at least 100 users "
            "in a year are listed, and these "
            f"{len(pairs)} appear in both years."
            if pairs
            else (
                "Same idea, for databases. Only databases with at least 100 "
                "users in a year are listed. None of those databases appear "
                "in more than one published year, so there is no year-over-year "
                "list here."
            )
        )
    else:
        ranked = sorted(
            [r for r in dbs if r["survey_year"] == years[0]] if years else [],
            key=lambda r: (r.get("usage_rank") is None, r.get("usage_rank") or 0),
        )[:12]
        source_rows = [
            {
                "name": r["tech_name"],
                "year": r["survey_year"],
                "value": int(r["total_users"]),
            }
            for r in ranked
        ]
        intro = (
            "Same idea, for databases. Only databases with at least 100 users "
            "in a year are listed."
        )
        pairs = []

    names_in_order = []
    seen = set()
    for row in source_rows:
        if row["name"] not in seen:
            names_in_order.append(row["name"])
            seen.add(row["name"])

    max_value = max((row["value"] for row in source_rows), default=1)
    rows = []
    for name in names_in_order:
        bars = []
        for year in years:
            match = next(
                (
                    row
                    for row in source_rows
                    if row["name"] == name and row["year"] == year
                ),
                None,
            )
            if match is None:
                continue
            bars.append(
                {
                    "year": year,
                    "value": match["value"],
                    "display": format_count(match["value"]),
                    "width_pct": bar_width_pct(match["value"], max_value),
                    "css": year_bar_class(year),
                }
            )
        rows.append(
            {
                "name": display_tech_name(name),
                "raw_name": name,
                "bars": bars,
            }
        )

    lead_sentence = ""
    change_sentence = ""
    if rows and years:
        firsts = []
        for year in years:
            year_rows = [
                r
                for r in dbs
                if r["survey_year"] == year
            ]
            year_rows = sorted(
                year_rows,
                key=lambda r: (r.get("usage_rank") is None, r.get("usage_rank") or 0),
            )
            if year_rows:
                firsts.append((year, display_tech_name(year_rows[0]["tech_name"])))
        if firsts and len(set(name for _, name in firsts)) == 1:
            name = firsts[0][1]
            if len(firsts) == 1:
                lead_sentence = f"{name} leads in {firsts[0][0]}."
            else:
                lead_sentence = f"{name} leads in both years."
        elif firsts:
            lead_sentence = (
                ", ".join(f"{name} leads in {year}" for year, name in firsts) + "."
            )

        if len(years) >= 2 and rows:
            earlier, later = years[0], years[-1]
            compared = 0
            lower = 0
            for row in rows:
                by_year = {bar["year"]: bar["value"] for bar in row["bars"]}
                if earlier in by_year and later in by_year:
                    compared += 1
                    if by_year[later] < by_year[earlier]:
                        lower += 1
            if compared and lower == compared:
                change_sentence = (
                    f"Every database on this list has a lower count in {later}."
                )
            elif compared:
                change_sentence = (
                    f"{lower} of {compared} databases have a lower count in {later}."
                )

    return {
        "rows": rows,
        "intro": intro,
        "lead_sentence": lead_sentence,
        "change_sentence": change_sentence,
        "show_key": bool(rows),
    }


def _salary_sublabel(
    country, experience_band, dev_type, remote_work, org_size
) -> str:
    parts = [display_country(country), experience_band, display_role(dev_type)]
    if remote_work:
        parts.append(str(remote_work))
    if org_size:
        parts.append(str(org_size))
    return " · ".join(p for p in parts if p)


def _role_is_fullstack_or_backend(dev_type: Optional[str]) -> bool:
    role = (display_role(dev_type) or "").lower()
    return "full-stack" in role or "back-end" in role or "backend" in role


def story_salaries(
    salary: list[dict[str, Any]], years: list[int]
) -> dict[str, Any]:
    salary_keys = (
        "country",
        "experience_band",
        "dev_type",
        "remote_work",
        "org_size",
    )
    paired = False
    items: list[dict[str, Any]] = []
    if len(years) >= 2:
        items = overlapping_pairs(salary, salary_keys, cap=12)
        paired = bool(items)
    if not items:
        # One published year, or two years with no overlapping cells: show
        # the first-seen cells (fetch already sorts by respondent_count DESC).
        grouped = index_rows(salary, salary_keys)
        for key, by_year in grouped.items():
            items.append({"key": key, "by_year": by_year})
            if len(items) >= 12:
                break

    groups = []
    all_medians = []
    lower = higher = unchanged = 0
    for i, item in enumerate(items, start=1):
        country, experience_band, dev_type, remote_work, org_size = item["key"]
        sub = _salary_sublabel(
            country, experience_band, dev_type, remote_work, org_size
        )
        bars = []
        by_year_values = {}
        for year in sorted(item["by_year"]):
            row = item["by_year"][year]
            value = row.get("median_salary")
            if value is None:
                continue
            by_year_values[year] = float(value)
            all_medians.append(float(value))
            bars.append(
                {
                    "year": year,
                    "value": float(value),
                    "display": format_money(value),
                    "css": year_bar_class(year),
                }
            )
        if (
            paired
            and len(years) >= 2
            and years[0] in by_year_values
            and years[-1] in by_year_values
        ):
            left = by_year_values[years[0]]
            right = by_year_values[years[-1]]
            if right < left:
                lower += 1
            elif right > left:
                higher += 1
            else:
                unchanged += 1
        groups.append(
            {
                "n": i,
                "sub": sub,
                "country": country,
                "currency_note": currency_wording(country),
                "us": is_united_states(country),
                "fs_be": _role_is_fullstack_or_backend(dev_type),
                "bars": bars,
            }
        )

    max_value = max(all_medians) if all_medians else 1
    for group in groups:
        for bar in group["bars"]:
            bar["width_pct"] = bar_width_pct(bar["value"], max_value)

    role_note = ""
    if groups and sum(1 for g in groups if g["fs_be"]) * 2 >= len(groups):
        role_note = (
            " The groups with the most data are mostly full-stack and "
            "back-end developers."
        )

    if not years:
        intro = (
            "Pay is shown as a median: half of the group earns more, half "
            "earns less. A group appears only if at least 5 people are in it."
        )
    elif len(years) < 2:
        intro = (
            "Pay is shown as a median: half of the group earns more, half "
            "earns less. People are grouped by country, years of coding "
            "professionally, role, remote work and company size, and a group "
            "appears only if at least 5 people are in it. Amounts are "
            f"{currency_wording(groups[0]['country']) if groups else 'in the currency people reported'}."
            f"{role_note} Year-over-year groups appear when two survey years "
            "are published."
        )
    else:
        us_all = bool(groups) and all(g["us"] for g in groups)
        currency = (
            "in the currency people reported, which for the United States is "
            "expected to be US dollars"
            if us_all
            else "in the currency people reported"
        )
        extra = ""
        if groups and not us_all and any(g["us"] for g in groups):
            extra = (
                " For groups in the United States, that is expected to be US dollars."
            )
        intro = (
            "Pay is shown as a median: half of the group earns more, half "
            "earns less. People are grouped by country, years of coding "
            "professionally, role, remote work and company size, and a group "
            "appears only if at least 5 people are in it. Amounts are "
            f"{currency}.{extra}{role_note}"
        )

    pair_note = ""
    if paired and groups:
        pair_note = (
            f"These {len(groups)} groups exist in both years. Several look "
            "alike because they differ in remote work or company size."
        )

    range_sentence = ""
    if all_medians:
        lo = min(all_medians)
        hi = max(all_medians)
        range_sentence = (
            f"medians sit between {format_money(lo)} and {format_money(hi)}."
        )

    change_sentence = ""
    if paired and groups and len(years) >= 2:
        n = len(groups)
        later = years[-1]
        earlier = years[0]
        change_sentence = (
            f"In {lower} of the {n} groups the {later} median is lower than "
            f"{earlier}, in {higher} it is higher"
        )
        if unchanged:
            change_sentence += f", in {unchanged} it is unchanged"
        change_sentence += "."

    return {
        "groups": groups,
        "intro": intro,
        "pair_note": pair_note,
        "range_sentence": range_sentence,
        "change_sentence": change_sentence,
        "lower": lower,
        "higher": higher,
        "unchanged": unchanged,
        "show_key": bool(groups),
        "min_cell_size": 5,
        "paired": paired,
    }


def humanize_ai_select(value: Optional[str]) -> str:
    if value is None or value == "":
        return "AI tool use not answered"
    return AI_SELECT_LABELS.get(value, value)


def humanize_ai_sent(value: Optional[str]) -> Optional[str]:
    if value is None or value == "" or str(value).lower() == "n/a":
        return None
    return f"{str(value).lower()} view"


def jobsat_sub_label(row: dict[str, Any]) -> str:
    bits = [humanize_ai_select(row.get("ai_select"))]
    sent = humanize_ai_sent(row.get("ai_sent"))
    if sent:
        bits.append(sent)
    return ", ".join(bits).rstrip(".")


def jobsat_full_key(row: dict[str, Any]) -> tuple:
    """Identity for a job-sat chart row: country, role, ai_select, ai_sent."""
    return (
        row.get("country"),
        row.get("dev_type"),
        row.get("ai_select"),
        row.get("ai_sent"),
    )


def sentence_country(name: Optional[str]) -> str:
    """Country name for a sentence: add 'the' for the US and the UK only."""
    shown = display_country(name)
    if shown in {"United States", "United Kingdom"}:
        return f"the {shown}"
    return shown


def _ai_threat_value(row: dict[str, Any]) -> Optional[str]:
    threat = row.get("ai_threat")
    if threat in (None, ""):
        return None
    return str(threat)


def story_jobsat(ai: list[dict[str, Any]], years: list[int]) -> dict[str, Any]:
    years_with_scores = []
    for year in years:
        rows = [
            r
            for r in ai
            if r["survey_year"] == year and r.get("avg_job_satisfaction") is not None
        ]
        if rows:
            years_with_scores.append(year)
    if not years_with_scores:
        return {
            "rows": [],
            "year": None,
            "range_sentence": "",
            "contrast_sentences": [],
            "not_asked": True,
            "intro": (
                "Job satisfaction was not asked in any published year on this page."
            ),
        }

    year = years_with_scores[-1]
    ranked = [
        r
        for r in ai
        if r["survey_year"] == year and r.get("avg_job_satisfaction") is not None
    ]
    ranked = sorted(ranked, key=lambda r: -int(r.get("respondent_count") or 0))[:8]

    peers_by_key: dict[tuple, list[int]] = {}
    for i, row in enumerate(ranked):
        peers_by_key.setdefault(jobsat_full_key(row), []).append(i)

    rows = []
    for i, row in enumerate(ranked):
        main = f"{display_country(row.get('country'))} · {display_role(row.get('dev_type'))}"
        clean_sub = jobsat_sub_label(row)
        chart_sub = clean_sub
        peers = peers_by_key[jobsat_full_key(row)]
        if len(peers) > 1:
            threat = _ai_threat_value(row)
            all_null = all(_ai_threat_value(ranked[j]) is None for j in peers)
            if threat is not None:
                chart_sub = f"{clean_sub}. answered the AI-threat question: {threat}"
            elif all_null:
                pos = peers.index(i) + 1
                chart_sub = f"{clean_sub} (group {pos} of {len(peers)})"
        score = float(row["avg_job_satisfaction"])
        rows.append(
            {
                "main": main,
                "sub": chart_sub,
                "clean_sub": clean_sub,
                "score": score,
                "display": format_score(score),
                "width_pct": bar_width_pct(score, 10.0),
                "css": year_bar_class(year),
                "year": year,
                "country": row.get("country"),
                "dev_type": row.get("dev_type"),
                "ai_select": row.get("ai_select"),
                "ai_sent": row.get("ai_sent"),
            }
        )

    range_sentence = ""
    if rows:
        lo = min(rows, key=lambda r: r["score"])
        hi = max(rows, key=lambda r: r["score"])
        range_sentence = (
            f"all {len(rows)} groups sit between {lo['display']} and {hi['display']}. "
            f"Lowest: {lo['main']} ({lo['clean_sub']}) at {lo['display']}. "
            f"Highest: {hi['main']} ({hi['clean_sub']}) at {hi['display']}."
        )

    contrast = []
    groups: dict[tuple, dict[str, list]] = {}
    for row in rows:
        key = (row["country"], row["dev_type"])
        bucket = groups.setdefault(key, {"yes": [], "no": []})
        select = str(row.get("ai_select") or "")
        if select.startswith("Yes"):
            bucket["yes"].append(row)
        elif select.startswith("No"):
            bucket["no"].append(row)
    for (country, dev_type), bucket in groups.items():
        if not bucket["yes"] or not bucket["no"]:
            continue
        no_row = bucket["no"][0]
        for yes_row in bucket["yes"]:
            view = humanize_ai_sent(yes_row.get("ai_sent")) or "unspecified view"
            contrast.append(
                "In "
                f"{sentence_country(country)}, {display_role(dev_type)} developers "
                f"who use AI tools ({view}) score {yes_row['display']}, against "
                f"{no_row['display']} for those who do not."
            )
            if len(contrast) >= 3:
                break
        if len(contrast) >= 3:
            break

    other_years = [y for y in years if y != year]
    only_later = (
        f" It is published for {year} only, because the "
        f"{other_years[0]} file does not contain that question."
        if len(years_with_scores) == 1 and other_years
        else f" It is published for {year}."
    )
    intro = (
        "Job satisfaction is an average score where higher means happier."
        + only_later
        + " The bars below are drawn on a scale from 0 to 10, so small "
        "differences look small on purpose."
    )
    return {
        "rows": rows,
        "year": year,
        "range_sentence": range_sentence,
        "contrast_sentences": contrast,
        "not_asked": False,
        "intro": intro,
    }


def year_question_missing(
    ai: list[dict[str, Any]], year: int, field: str
) -> bool:
    rows = [r for r in ai if r["survey_year"] == year]
    if not rows:
        return True
    return all(r.get(field) is None for r in rows)


def story_ai_chapter(ai: list[dict[str, Any]], years: list[int]) -> dict[str, Any]:
    not_asked = []
    for year in years:
        threat_missing = year_question_missing(ai, year, "pct_see_ai_as_threat")
        job_missing = year_question_missing(ai, year, "avg_job_satisfaction")
        if threat_missing and job_missing:
            not_asked.append(year)
    if not_asked:
        if len(not_asked) == 1:
            year = not_asked[0]
            sentence = (
                f"The {year} survey did not ask whether AI is a threat or how "
                "satisfied people are with their job, so there is nothing to "
                f"compare for {year}. The AI answers that exist are in Chapter 4."
            )
        else:
            listed = " and ".join(str(y) for y in not_asked)
            sentence = (
                f"The {listed} surveys did not ask whether AI is a threat or "
                "how satisfied people are with their job. The AI answers that "
                "exist are in Chapter 4."
            )
    else:
        sentence = (
            "Job satisfaction answers that exist are in Chapter 4. This page "
            "does not chart the share who see AI as a threat."
        )
    dropped = (
        'The earlier version of this page also showed bars of 0% and 100% for '
        '"see AI as a threat". Those were an artifact of how the groups were '
        "built, not real opinions, so they are left out until the table is fixed."
    )
    return {
        "not_asked_years": not_asked,
        "sentence": sentence,
        "dropped_chart_sentence": dropped,
    }


def story_title(years: list[int]) -> str:
    if not years:
        return "What developers told Stack Overflow"
    if len(years) == 1:
        return f"What developers told Stack Overflow in {years[0]}"
    if len(years) == 2:
        return f"What developers told Stack Overflow in {years[0]} and {years[1]}"
    body = ", ".join(str(y) for y in years[:-1]) + f", and {years[-1]}"
    return f"What developers told Stack Overflow in {body}"


def published_date_label(years_meta: list[dict[str, Any]]) -> Optional[str]:
    latest = None
    for meta in years_meta:
        ts = meta.get("published_at")
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    if latest is None:
        return None
    if isinstance(latest, datetime):
        return latest.date().isoformat()
    if isinstance(latest, date):
        return latest.isoformat()
    return str(latest)[:10]


def build_story_context(data: dict[str, Any]) -> dict[str, Any]:
    """Build the Jinja context from fetched lists of dicts. No Postgres."""
    years_meta = list(data.get("years") or [])
    year_list = [int(y["survey_year"]) for y in years_meta]
    salary = list(data.get("salary") or [])
    tech = list(data.get("tech") or [])
    ai = list(data.get("ai") or [])

    respondents = story_respondents(years_meta)
    languages = story_languages(tech, year_list)
    databases = story_databases(tech, year_list)
    salaries = story_salaries(salary, year_list)
    jobsat = story_jobsat(ai, year_list)
    ai_chapter = story_ai_chapter(ai, year_list)
    as_of = published_date_label(years_meta)

    salary_by_year = {}
    for row in salary:
        year = int(row["survey_year"])
        salary_by_year[year] = salary_by_year.get(year, 0) + int(
            row.get("respondent_count") or 0
        )

    year_pages = []
    for meta in years_meta:
        year = int(meta["survey_year"])
        year_pages.append(
            {
                "survey_year": year,
                "release_id": meta.get("release_id"),
                "release_id_short": _short_uuid(meta["release_id"])
                if meta.get("release_id")
                else "",
                "published_at": meta.get("published_at"),
                "salary_cell_respondents": salary_by_year.get(year, 0),
            }
        )

    yoy_enabled = len(year_list) >= 2
    if not year_list:
        lede = (
            "Nothing is published yet. dwh.active_release is empty, so the "
            "views have no rows."
        )
    elif not yoy_enabled:
        lede = (
            "Every year Stack Overflow asks developers around the world about "
            "their work and tools. This page tells the story of the published "
            f"{year_list[0]} survey. Year-over-year charts appear when two or "
            "more survey years are published. Only "
            f"{year_list[0]} is live in dwh.active_release, so this page "
            "does not invent a second year."
        )
    else:
        lede = (
            "Every year Stack Overflow asks developers around the world about "
            "their work and tools. This page tells the story of those surveys. "
            "A data pipeline cleaned the raw answers and checked them before "
            "anything was shown here."
        )

    return {
        "title": story_title(year_list),
        "lede": lede,
        "years": year_pages,
        "year_list": year_list,
        "year_key": [
            {"year": year, "css": year_bar_class(year)} for year in year_list
        ],
        "nothing_published": len(year_list) == 0,
        "yoy_enabled": yoy_enabled,
        "data_as_of": as_of or "nothing published",
        "respondents": respondents,
        "languages": languages,
        "databases": databases,
        "salaries": salaries,
        "jobsat": jobsat,
        "ai_chapter": ai_chapter,
        "published_date_line": (
            f"Published snapshot dated {as_of}." if as_of else ""
        ),
    }


def fetch_published(conn) -> dict[str, Any]:
    """Read only v_* views plus dwh.active_release. Never marts.mart_*."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.survey_year,
                   a.release_id::text,
                   r.published_at,
                   NULLIF(r.dq_summary->>'total_rows_loaded', '')::numeric
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
    for survey_year, release_id, published_at, respondents in active:
        years.append(
            {
                "survey_year": int(survey_year),
                "release_id": release_id,
                "release_id_short": _short_uuid(release_id),
                "published_at": published_at,
                "respondents": int(respondents) if respondents is not None else None,
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
                "label": f"{row[1]} · {row[2]} · {row[3] or 'n/a'} / {row[4] or 'n/a'}",
            }
        )

    return {
        "years": years,
        "salary": salary_rows,
        "tech": tech_rows,
        "ai": ai_rows,
    }


def build_context(conn) -> dict[str, Any]:
    return build_story_context(fetch_published(conn))


def render_html(ctx: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    return env.get_template("index.html.j2").render(**ctx)


def render(out_dir: Optional[Path] = None) -> Path:
    """Query the warehouse and write docs/site/index.html (or out_dir)."""
    dest = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".nojekyll").write_text("", encoding="utf-8")
    # GitHub Pages "Deploy from /docs" looks here, not only in docs/site/.
    if dest.resolve() == DEFAULT_OUT_DIR.resolve():
        (DEFAULT_OUT_DIR.parent / ".nojekyll").write_text("", encoding="utf-8")

    conn = get_connection()
    try:
        ctx = build_context(conn)
    finally:
        conn.close()

    html_out = render_html(ctx)
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
    parser = argparse.ArgumentParser(
        description="Render docs/site from published marts.v_*"
    )
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
