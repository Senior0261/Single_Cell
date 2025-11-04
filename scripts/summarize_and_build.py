"""Generate daily content artefacts and static pages for the site."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

CONTENT_DIR = Path("content")
OUTPUT_ROOT = Path(".")
ASSETS_DIR = Path("assets")
TEMPLATES_DIR = Path("templates")
SITE_URL_ENV = "SITE_BASE_URL"
DEFAULT_SITE_URL = "https://example.com"
SITE_TITLE = "Single-Cell Omics Daily"
SITE_DESCRIPTION = "Daily roundup of single-cell omics papers, tools, and datasets."


@dataclass
class Highlight:
    type: str
    title: str
    authors: List[str]
    venue: str | None
    year: int | None
    doi: str | None
    url: str
    accession: str | None
    tags: List[str]
    summary: str
    why_it_matters: str
    source: str
    published: str | None

    @property
    def date(self) -> dt.date:
        if self.published:
            return dt.date.fromisoformat(self.published)
        return dt.date.today()


@dataclass
class DailyPayload:
    date: dt.date
    generated_at: dt.datetime
    highlights: List[Highlight]


def load_payloads() -> List[DailyPayload]:
    payloads: List[DailyPayload] = []
    for path in sorted(CONTENT_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        highlights = [Highlight(**item) for item in data.get("highlights", [])]
        payloads.append(
            DailyPayload(
                date=dt.date.fromisoformat(data["date"]),
                generated_at=dt.datetime.fromisoformat(data["generated_at"].replace("Z", "")),
                highlights=highlights,
            )
        )
    return payloads


def ensure_markdown(payload: DailyPayload) -> Path:
    CONTENT_DIR.mkdir(exist_ok=True)
    markdown_path = CONTENT_DIR / f"{payload.date.isoformat()}.md"
    lines = [f"# {payload.date:%B %d, %Y}", ""]
    for item in payload.highlights:
        authors = ", ".join(item.authors) if item.authors else "Unknown"
        lines.extend(
            [
                f"## {item.title}",
                f"*Type*: {item.type.title()}  ",
                f"*Authors*: {authors}  ",
                f"*Venue*: {item.venue or '—'}  ",
                f"*Year*: {item.year or '—'}  ",
                f"*DOI*: {item.doi or '—'}  ",
                f"*URL*: {item.url}",
                "",
                item.summary,
                "",
                f"**Why it matters**: {item.why_it_matters}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return markdown_path


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["slugify"] = slugify
    return env


def aggregate_entries(payloads: Sequence[DailyPayload]) -> List[Highlight]:
    items: List[Highlight] = []
    for payload in payloads:
        items.extend(payload.highlights)
    items.sort(key=lambda item: item.date, reverse=True)
    return items


def group_by_date(payloads: Sequence[DailyPayload]) -> dict[dt.date, List[Highlight]]:
    grouped: dict[dt.date, List[Highlight]] = defaultdict(list)
    for payload in payloads:
        grouped[payload.date].extend(payload.highlights)
    return dict(sorted(grouped.items(), key=lambda x: x[0], reverse=True))


def group_by_tag(items: Sequence[Highlight]) -> dict[str, List[Highlight]]:
    grouped: dict[str, List[Highlight]] = defaultdict(list)
    for item in items:
        for tag in item.tags:
            grouped[tag].append(item)
    return dict(sorted(grouped.items()))


def build_templates(payloads: Sequence[DailyPayload]) -> list[Path]:
    env = build_environment()
    site_url = os.getenv(SITE_URL_ENV, DEFAULT_SITE_URL).rstrip("/")
    items = aggregate_entries(payloads)
    grouped = group_by_date(payloads)
    tags = group_by_tag(items)

    template_context = {
        "site_title": SITE_TITLE,
        "site_description": SITE_DESCRIPTION,
        "site_url": site_url,
        "latest": items[: min(25, len(items))],
        "grouped": grouped,
        "tags": tags,
    }

    generated: list[Path] = []
    for name in ["index.html", "archive.html", "tags.html"]:
        template = env.get_template(name)
        output = template.render(**template_context)
        path = OUTPUT_ROOT / name
        path.write_text(output, encoding="utf-8")
        generated.append(path)

    feed_template = env.get_template("feed.xml")
    feed_output = feed_template.render(**template_context, generated=dt.datetime.utcnow())
    feed_path = OUTPUT_ROOT / "feed.xml"
    feed_path.write_text(feed_output, encoding="utf-8")
    generated.append(feed_path)

    sitemap_template = env.get_template("sitemap.xml")
    sitemap_output = sitemap_template.render(**template_context)
    sitemap_path = OUTPUT_ROOT / "sitemap.xml"
    sitemap_path.write_text(sitemap_output, encoding="utf-8")
    generated.append(sitemap_path)

    robots_template = env.get_template("robots.txt")
    robots_output = robots_template.render(**template_context)
    robots_path = OUTPUT_ROOT / "robots.txt"
    robots_path.write_text(robots_output, encoding="utf-8")
    generated.append(robots_path)

    search_index = build_search_index(items)
    search_path = ASSETS_DIR / "search-index.json"
    ASSETS_DIR.mkdir(exist_ok=True)
    search_path.write_text(json.dumps(search_index, indent=2), encoding="utf-8")
    generated.append(search_path)

    return generated


def build_search_index(items: Sequence[Highlight]) -> list[dict[str, str]]:
    index = []
    for item in items:
        index.append(
            {
                "title": item.title,
                "url": item.url,
                "summary": item.summary,
                "why_it_matters": item.why_it_matters,
                "tags": ", ".join(item.tags),
            }
        )
    return index


def export_table(payloads: Sequence[DailyPayload]) -> Path:
    rows = []
    for payload in payloads:
        for item in payload.highlights:
            rows.append(
                {
                    "date": payload.date.isoformat(),
                    "type": item.type,
                    "title": item.title,
                    "authors": "; ".join(item.authors),
                    "venue": item.venue,
                    "year": item.year,
                    "doi": item.doi,
                    "url": item.url,
                    "accession": item.accession,
                    "tags": "; ".join(item.tags),
                }
            )
    df = pd.DataFrame(rows)
    csv_path = CONTENT_DIR / "highlights.csv"
    df.to_csv(csv_path, index=False)
    return csv_path


def summarise(payloads: Sequence[DailyPayload]) -> None:
    generated_files: list[Path] = []
    for payload in payloads:
        generated_files.append(ensure_markdown(payload))
    generated_files.extend(build_templates(payloads))
    generated_files.append(export_table(payloads))

    print("Generated artefacts:")
    for path in generated_files:
        print(f" - {path}")


def load_latest_payloads(window: int) -> List[DailyPayload]:
    payloads = load_payloads()
    if window <= 0:
        return payloads
    cutoff = dt.date.today() - dt.timedelta(days=window - 1)
    return [payload for payload in payloads if payload.date >= cutoff]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build pages from collected highlights")
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Number of days to include in rendered pages.",
    )
    args = parser.parse_args()

    payloads = load_latest_payloads(args.window)
    if not payloads:
        raise SystemExit("No payloads available. Run fetch_sources first.")

    summarise(payloads)


if __name__ == "__main__":
    main()
