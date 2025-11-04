"""Fetch daily single-cell omics highlights from multiple public sources.

This module queries CrossRef, Europe PMC, arXiv and GitHub repositories for
recent activity related to single-cell omics.  Results are normalised into a
common schema which can be consumed by ``summarize_and_build``.

All network access happens through ``requests`` and is wrapped in retry logic
using :mod:`tenacity` to make the workflow resilient to transient failures.

Environment variables
---------------------
``CROSSREF_MAILTO``
    Optional email address passed to the CrossRef polite pool.
``GH_READ_TOKEN``
    Optional token with ``repo`` scope for GitHub search.  Falls back to the
    workflow ``GITHUB_TOKEN`` when available.
``GITHUB_TOKEN``
    Token automatically provided inside GitHub Actions.  Used if
    ``GH_READ_TOKEN`` is not present.

The script writes a JSON file under ``content/YYYY-MM-DD.json`` with a top level
payload of the form::

    {
        "date": "2024-05-01",
        "generated_at": "2024-05-01T15:03:12Z",
        "highlights": [ ... normalised items ... ]
    }

Each item contains at least the following keys: ``type``, ``title``, ``authors``,
``venue``, ``year``, ``doi``, ``url``, ``accession``, ``tags``, ``summary`` and
``why_it_matters``.  Summaries are intentionally lightweight to guarantee the
workflow produces some contextual text even when source metadata is sparse.

The module can be executed as a script::

    python scripts/fetch_sources.py --days 7

which fetches data for the last seven days by default.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, List, Optional

import feedparser
import requests
from dateutil import parser as dateparser
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEYWORDS = [
    "single-cell",
    "scRNA-seq",
    "scATAC-seq",
    "CITE-seq",
    "multiome",
    "spatial transcriptomics",
    "Visium",
    "MERFISH",
    "seqFISH",
    "integrative analysis",
]

MAX_RESULTS_PER_SOURCE = 30
USER_AGENT = "singlecell-daily-bot/1.0"


class Highlight(BaseModel):
    """Normalised schema for a single highlight entry."""

    type: str = Field(description="Type of entry, e.g. paper/tool/dataset")
    title: str
    authors: List[str] = Field(default_factory=list)
    venue: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    url: str
    accession: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    summary: Optional[str] = None
    why_it_matters: Optional[str] = None
    source: str = Field(description="Source identifier, e.g. crossref")
    published: Optional[str] = Field(
        default=None,
        description="ISO formatted publication date when available",
    )


@dataclass
class FetchContext:
    start_date: dt.date
    end_date: dt.date
    session: requests.Session

    @property
    def crossref_mailto(self) -> Optional[str]:
        return os.getenv("CROSSREF_MAILTO")

    @property
    def github_token(self) -> Optional[str]:
        return os.getenv("GH_READ_TOKEN") or os.getenv("GITHUB_TOKEN")


def daterange(days: int) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    return start, end


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def configure_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    token = os.getenv("GH_READ_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        session.headers.setdefault("Authorization", f"Bearer {token}")
    return session


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def http_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=30, **kwargs)
    response.raise_for_status()
    return response


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_crossref(ctx: FetchContext) -> Iterable[Highlight]:
    logging.info("Fetching CrossRef records")
    params = {
        "filter": f"from-pub-date:{ctx.start_date},until-pub-date:{ctx.end_date}",
        "rows": MAX_RESULTS_PER_SOURCE,
        "sort": "published",
        "order": "desc",
    }
    if ctx.crossref_mailto:
        params["mailto"] = ctx.crossref_mailto

    highlights: list[Highlight] = []
    for keyword in KEYWORDS:
        local_params = {"query": keyword, **params}
        resp = http_get(ctx.session, "https://api.crossref.org/works", params=local_params)
        data = resp.json()
        for item in data.get("message", {}).get("items", []):
            title = " ".join(item.get("title") or []).strip()
            if not title:
                continue
            authors = [
                " ".join(filter(None, [a.get("given"), a.get("family")])).strip()
                for a in item.get("author", [])
            ]
            published = item.get("published-print") or item.get("published-online")
            date_parts = published.get("date-parts", [[None]]) if published else [[None]]
            year = date_parts[0][0] if date_parts else None
            doi = item.get("DOI")
            url = item.get("URL")
            highlight = Highlight(
                type="paper",
                title=title,
                authors=[a for a in authors if a],
                venue=(item.get("container-title") or [None])[0],
                year=year,
                doi=doi,
                url=url or f"https://doi.org/{doi}" if doi else None,
                accession=None,
                tags=["single-cell", "literature", keyword],
                summary=item.get("subtitle", [None])[0],
                why_it_matters="Recent publication indexed via CrossRef.",
                source="crossref",
                published=dateparser.parse(item.get("created", {}).get("date-time", ""), ignoretz=True).date().isoformat()
                if item.get("created", {}).get("date-time")
                else None,
            )
            if highlight.url:
                highlights.append(highlight)
    return highlights


def fetch_europe_pmc(ctx: FetchContext) -> Iterable[Highlight]:
    logging.info("Fetching Europe PMC records")
    highlights: list[Highlight] = []
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    for keyword in KEYWORDS:
        query = f"{keyword} AND FIRST_PDATE:[{ctx.start_date} TO {ctx.end_date}]"
        params = {
            "query": query,
            "pageSize": MAX_RESULTS_PER_SOURCE,
            "format": "json",
        }
        resp = http_get(ctx.session, base_url, params=params)
        data = resp.json()
        for result in data.get("resultList", {}).get("result", []):
            title = (result.get("title") or "").strip()
            if not title:
                continue
            authors = []
            if result.get("authorString"):
                authors = [a.strip() for a in result["authorString"].split(",") if a.strip()]
            doi = result.get("doi")
            url = result.get("fullTextUrlList", {}).get("fullTextUrl", [])
            url = url[0]["url"] if url else result.get("pubmedUrl") or result.get("pmcid")
            highlight = Highlight(
                type="paper",
                title=title,
                authors=authors,
                venue=result.get("journalTitle"),
                year=int(result["pubYear"]) if result.get("pubYear") else None,
                doi=doi,
                url=url,
                accession=result.get("pmcid") or result.get("id"),
                tags=["single-cell", "literature", keyword],
                summary=result.get("abstractText"),
                why_it_matters="Latest biomedical preprint or article from Europe PMC.",
                source="europe_pmc",
                published=result.get("firstPublicationDate"),
            )
            if highlight.url:
                highlights.append(highlight)
    return highlights


def fetch_arxiv(ctx: FetchContext) -> Iterable[Highlight]:
    logging.info("Fetching arXiv records")
    highlights: list[Highlight] = []
    base_url = "https://export.arxiv.org/api/query"
    for keyword in KEYWORDS:
        params = {
            "search_query": f"all:{keyword}",
            "start": 0,
            "max_results": 20,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        resp = http_get(ctx.session, base_url, params=params)
        feed = feedparser.parse(resp.text)
        for entry in feed.entries:
            if not entry.get("title"):
                continue
            published = dateparser.parse(entry.get("published")) if entry.get("published") else None
            if published and not (ctx.start_date <= published.date() <= ctx.end_date):
                continue
            authors = [a.get("name") for a in entry.get("authors", []) if a.get("name")]
            summary = entry.get("summary")
            highlight = Highlight(
                type="preprint",
                title=entry.get("title").strip(),
                authors=authors,
                venue="arXiv",
                year=published.year if published else None,
                doi=None,
                url=entry.get("link"),
                accession=entry.get("id"),
                tags=["single-cell", "preprint", keyword],
                summary=summary,
                why_it_matters="Recent arXiv submission related to single-cell omics.",
                source="arxiv",
                published=published.date().isoformat() if published else None,
            )
            highlights.append(highlight)
    return highlights


def fetch_github(ctx: FetchContext) -> Iterable[Highlight]:
    logging.info("Fetching GitHub repositories")
    highlights: list[Highlight] = []
    token = ctx.github_token
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for keyword in KEYWORDS:
        query = f"{keyword} created:{ctx.start_date}..{ctx.end_date}".replace(" ", "+")
        params = {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": 30,
        }
        resp = http_get(ctx.session, "https://api.github.com/search/repositories", params=params, headers=headers)
        data = resp.json()
        for repo in data.get("items", []):
            highlight = Highlight(
                type="tool",
                title=repo.get("full_name"),
                authors=[repo.get("owner", {}).get("login")],
                venue="GitHub",
                year=dt.date.fromisoformat(repo.get("created_at"[:10])).year if repo.get("created_at") else None,
                doi=None,
                url=repo.get("html_url"),
                accession=str(repo.get("id")) if repo.get("id") else None,
                tags=["single-cell", "software", keyword],
                summary=(repo.get("description") or "Single-cell omics related repository."),
                why_it_matters="New or trending GitHub project in the single-cell ecosystem.",
                source="github",
                published=repo.get("created_at", "")[:10] or None,
            )
            highlights.append(highlight)
    return highlights


FETCHERS = [fetch_crossref, fetch_europe_pmc, fetch_arxiv, fetch_github]


def deduplicate(highlights: Iterable[Highlight]) -> list[Highlight]:
    seen = {}
    for item in highlights:
        key_material = item.doi or item.url or item.title
        digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen[digest] = item
    return list(seen.values())


def enrich_summaries(items: Iterable[Highlight]) -> Iterable[Highlight]:
    for item in items:
        if not item.summary:
            snippet_parts = []
            if item.venue:
                snippet_parts.append(f"Published in {item.venue}.")
            if item.authors:
                snippet_parts.append(f"Lead authors include {item.authors[0]}.")
            snippet_parts.append("Highlights advances in single-cell omics.")
            item.summary = " ".join(snippet_parts)
        if not item.why_it_matters:
            item.why_it_matters = (
                "Provides actionable insight for researchers tracking single-cell omics developments."
            )
        yield item


def bucket_by_date(items: Iterable[Highlight]) -> dict[str, list[Highlight]]:
    buckets: dict[str, list[Highlight]] = defaultdict(list)
    for item in items:
        pub_date = item.published or dt.date.today().isoformat()
        buckets[pub_date].append(item)
    return buckets


def write_payload(date_key: str, highlights: list[Highlight]) -> str:
    payload = {
        "date": date_key,
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "highlights": [item.dict() for item in highlights],
    }
    output_path = os.path.join("content", f"{date_key}.json")
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return output_path


def collect(days: int) -> list[str]:
    session = configure_session()
    start, end = daterange(days)
    ctx = FetchContext(start_date=start, end_date=end, session=session)
    combined: list[Highlight] = []
    for fetcher in FETCHERS:
        try:
            combined.extend(list(fetcher(ctx)))
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.exception("Fetcher %s failed: %s", fetcher.__name__, exc)
    unique_items = deduplicate(enrich_summaries(combined))
    buckets = bucket_by_date(unique_items)
    written_files = []
    for date_key, items in buckets.items():
        written_files.append(write_payload(date_key, sorted(items, key=lambda x: x.title.lower())))
    return written_files


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch single-cell omics highlights")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    written = collect(args.days)
    if not written:
        logging.warning("No highlights were collected for the requested window.")
    else:
        logging.info("Written %d payload(s):", len(written))
        for path in written:
            logging.info(" - %s", path)


if __name__ == "__main__":
    main()
