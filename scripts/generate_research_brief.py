
#!/usr/bin/env python3
"""Generate and optionally email a personalized research brief.

Dependency-free by design: this script uses only the Python standard library so
it can run reliably in GitHub Actions without package installation.
"""

from __future__ import annotations

import argparse
import datetime as dt
from email.message import EmailMessage
import html
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.getenv("RESEARCH_BRIEF_CONFIG", ROOT / "automation" / "research_brief_config.json"))
BRIEF_DIR = Path(os.getenv("RESEARCH_BRIEF_DIR", ROOT / "research_briefs"))
APP_NAME = "research-brief-actions/1.0"
UTC = dt.timezone.utc


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def contact_email(config: dict[str, Any]) -> str:
    return os.getenv("RESEARCH_BRIEF_CONTACT_EMAIL") or config.get("contact_email") or config.get("recipient_email", "researcher@example.com")


def user_agent(config: dict[str, Any]) -> str:
    return f"{APP_NAME} (mailto:{contact_email(config)})"


def normalize_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(normalize_text(v) for v in value)
    if value is None:
        return ""
    return html.unescape(re.sub(r"\s+", " ", str(value)).strip())


def http_json(url: str, config: dict[str, Any], *, timeout: int = 25) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(config)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: failed to fetch {url}: {exc}", file=sys.stderr)
        return None


def http_text(url: str, config: dict[str, Any], *, timeout: int = 25) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(config)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: failed to fetch {url}: {exc}", file=sys.stderr)
        return None


def date_from_parts(parts: Any) -> str:
    try:
        date_parts = parts["date-parts"][0]
        year = int(date_parts[0])
        month = int(date_parts[1]) if len(date_parts) > 1 else 1
        day = int(date_parts[2]) if len(date_parts) > 2 else 1
        return dt.date(year, month, day).isoformat()
    except Exception:
        return ""


def parse_published_date(value: str) -> dt.date | None:
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"\d{4}(?:-\d{2})?(?:-\d{2})?", text)
    if not match:
        return None
    parts = match.group(0).split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return dt.date(year, month, day)
    except ValueError:
        return None


def is_within_window(paper: dict[str, Any], days_back: int, run_date: dt.date) -> bool:
    published = parse_published_date(paper.get("published", ""))
    if published is None:
        return True
    return run_date - dt.timedelta(days=days_back) <= published <= run_date + dt.timedelta(days=1)


def recency_boost(paper: dict[str, Any], days_back: int, run_date: dt.date) -> float:
    published = parse_published_date(paper.get("published", ""))
    if published is None:
        return 0.0
    age_days = (run_date - published).days
    if age_days < 0 or age_days > days_back:
        return 0.0
    if days_back <= 0:
        return 0.0
    return round(0.8 * (1 - age_days / days_back), 2)


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:160]


def paper_key(paper: dict[str, Any]) -> str:
    doi = normalize_text(paper.get("doi")).lower()
    if doi:
        return f"doi:{doi}"
    return f"title:{title_key(paper.get('title', ''))}"


def authors_crossref(author_list: list[dict[str, Any]]) -> str:
    names: list[str] = []
    for author in author_list[:8]:
        name = normalize_text(f"{author.get('given', '')} {author.get('family', '')}")
        if name:
            names.append(name)
    if len(author_list) > 8:
        names.append("et al.")
    return ", ".join(names) or "Unknown"


def affiliations_crossref(author_list: list[dict[str, Any]]) -> str:
    affs: list[str] = []
    for author in author_list:
        for aff in author.get("affiliation", []) or []:
            name = normalize_text(aff.get("name"))
            if name and name not in affs:
                affs.append(name)
    return "; ".join(affs[:6]) or "Metadata unavailable"


def authors_openalex(authorships: list[dict[str, Any]]) -> str:
    names = [normalize_text(a.get("author", {}).get("display_name")) for a in authorships[:8]]
    names = [n for n in names if n]
    if len(authorships) > 8:
        names.append("et al.")
    return ", ".join(names) or "Unknown"


def affiliations_openalex(authorships: list[dict[str, Any]]) -> str:
    affs: list[str] = []
    for authorship in authorships:
        for institution in authorship.get("institutions", []) or []:
            name = normalize_text(institution.get("display_name"))
            if name and name not in affs:
                affs.append(name)
    return "; ".join(affs[:6]) or "Metadata unavailable"


def inverted_index_to_text(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return normalize_text(" ".join(word for _, word in sorted(words)))


def fetch_crossref(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = (dt.datetime.now(UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", []))
    for query in queries:
        params = {
            "query.bibliographic": query,
            "filter": f"from-index-date:{since},type:journal-article",
            "sort": "indexed",
            "order": "desc",
            "rows": "20",
            "select": "DOI,title,author,container-title,published-print,published-online,indexed,URL,abstract,subject,publisher",
        }
        data = http_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("message", {}).get("items", []):
            title = normalize_text(item.get("title"))
            if not title:
                continue
            papers.append({
                "title": title,
                "authors": authors_crossref(item.get("author", []) or []),
                "affiliations": affiliations_crossref(item.get("author", []) or []),
                "venue": normalize_text(item.get("container-title")) or normalize_text(item.get("publisher")),
                "source": "Crossref",
                "published": date_from_parts(item.get("published-online")) or date_from_parts(item.get("published-print")) or date_from_parts(item.get("indexed")),
                "doi": normalize_text(item.get("DOI")),
                "url": normalize_text(item.get("URL")),
                "abstract": normalize_text(item.get("abstract")),
                "subjects": ", ".join(item.get("subject", [])[:8]) if isinstance(item.get("subject"), list) else "",
            })
        time.sleep(0.15)
    return papers


def fetch_openalex(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = (dt.datetime.now(UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", []))
    for query in queries:
        params = {
            "search": query,
            "filter": f"from_publication_date:{since},type:article",
            "sort": "publication_date:desc",
            "per-page": "20",
            "mailto": contact_email(config),
        }
        data = http_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("results", []):
            title = normalize_text(item.get("title") or item.get("display_name"))
            if not title:
                continue
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            papers.append({
                "title": title,
                "authors": authors_openalex(item.get("authorships", []) or []),
                "affiliations": affiliations_openalex(item.get("authorships", []) or []),
                "venue": normalize_text(source.get("display_name")),
                "source": "OpenAlex",
                "published": normalize_text(item.get("publication_date")),
                "doi": normalize_text((item.get("doi") or "").replace("https://doi.org/", "")),
                "url": normalize_text(primary.get("landing_page_url") or item.get("id")),
                "abstract": inverted_index_to_text(item.get("abstract_inverted_index")),
                "subjects": ", ".join(c.get("display_name", "") for c in item.get("concepts", [])[:8]),
            })
        time.sleep(0.15)
    return papers

def fetch_ieee(config: dict[str, Any]) -> list[dict[str, Any]]:
    api_key = os.getenv("IEEE_XPLORE_API_KEY")
    if not api_key:
        return []
    papers: list[dict[str, Any]] = []
    for query in config.get("query_templates", []):
        params = {
            "apikey": api_key,
            "format": "json",
            "max_records": "20",
            "sort_order": "desc",
            "sort_field": "publication_year",
            "querytext": query,
        }
        data = http_json("https://ieeexploreapi.ieee.org/api/v1/search/articles?" + urllib.parse.urlencode(params), config)
        for item in (data or {}).get("articles", []):
            papers.append({
                "title": normalize_text(item.get("title")),
                "authors": normalize_text(item.get("authors", {}).get("authors", [])),
                "affiliations": "Metadata unavailable",
                "venue": normalize_text(item.get("publication_title")),
                "source": "IEEE Xplore",
                "published": normalize_text(item.get("publication_year")),
                "doi": normalize_text(item.get("doi")),
                "url": normalize_text(item.get("html_url") or item.get("pdf_url")),
                "abstract": normalize_text(item.get("abstract")),
                "subjects": normalize_text(item.get("index_terms")),
            })
        time.sleep(0.25)
    return [p for p in papers if p.get("title")]


def fetch_arxiv(config: dict[str, Any], days_back: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    since = dt.datetime.now(UTC) - dt.timedelta(days=days_back)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    for query in config.get("query_templates", []):
        params = {
            "search_query": f"all:{query}",
            "start": "0",
            "max_results": "20",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        data = http_text("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params), config)
        if not data:
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            print(f"warning: failed to parse arXiv response for {query}: {exc}", file=sys.stderr)
            continue
        for entry in root.findall("atom:entry", ns):
            published = normalize_text(entry.findtext("atom:published", default="", namespaces=ns))
            try:
                published_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                published_dt = None
            if published_dt and published_dt < since:
                continue
            title = normalize_text(entry.findtext("atom:title", default="", namespaces=ns))
            if not title:
                continue
            authors = [
                normalize_text(author.findtext("atom:name", default="", namespaces=ns))
                for author in entry.findall("atom:author", ns)
            ]
            categories = [
                normalize_text(category.attrib.get("term"))
                for category in entry.findall("atom:category", ns)
                if category.attrib.get("term")
            ]
            doi = normalize_text(entry.findtext("arxiv:doi", default="", namespaces=ns))
            papers.append({
                "title": title,
                "authors": ", ".join(a for a in authors if a) or "Unknown",
                "affiliations": "Metadata unavailable",
                "venue": "arXiv",
                "source": "arXiv",
                "published": published[:10],
                "doi": doi,
                "url": normalize_text(entry.findtext("atom:id", default="", namespaces=ns)),
                "abstract": normalize_text(entry.findtext("atom:summary", default="", namespaces=ns)),
                "subjects": ", ".join(categories),
            })
        time.sleep(0.25)
    return papers


def dedupe(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for paper in papers:
        key = paper_key(paper)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(paper)
    return out


def seen_papers_path() -> Path:
    return BRIEF_DIR / "seen_papers.json"


def load_seen_papers() -> set[str]:
    path = seen_papers_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: failed to read {display_path(path)}: {exc}", file=sys.stderr)
        return set()
    if not isinstance(data, list):
        print(f"warning: ignoring malformed {display_path(path)}", file=sys.stderr)
        return set()
    return {str(item) for item in data if item}


def remove_seen_papers(papers: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    return [paper for paper in papers if paper_key(paper) not in seen]


def update_seen_papers(papers: list[dict[str, Any]]) -> None:
    existing = load_seen_papers()
    updated = existing | {paper_key(paper) for paper in papers if paper_key(paper)}
    BRIEF_DIR.mkdir(exist_ok=True)
    seen_papers_path().write_text(json.dumps(sorted(updated), indent=2) + "\n", encoding="utf-8")


def paper_text(paper: dict[str, Any]) -> str:
    return " ".join(paper.get(k, "") for k in ("title", "abstract", "venue", "subjects")).lower()


def score_paper(paper: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    profile = config.get("research_profile", {})
    haystack = paper_text(paper)
    score = 0.0
    reasons: list[str] = []
    for keyword in profile.get("core_topics", []):
        if keyword.lower() in haystack:
            score += 2.2
            reasons.append(keyword)
    for keyword in profile.get("method_keywords", []):
        if keyword.lower() in haystack:
            score += 1.3
            reasons.append(keyword)
    for keyword in profile.get("objective_keywords", []):
        if keyword.lower() in haystack:
            score += 1.0
            reasons.append(keyword)
    for journal in config.get("journals", []):
        if journal.lower() in paper.get("venue", "").lower():
            score += 3.0
            reasons.append(journal)
    return round(score, 2), sorted(set(reasons), key=str.lower)[:12]


def rank_papers(papers: list[dict[str, Any]], config: dict[str, Any], days_back: int, run_date: dt.date) -> list[dict[str, Any]]:
    for paper in papers:
        score, reasons = score_paper(paper, config)
        boost = recency_boost(paper, days_back, run_date)
        paper["score"] = round(score + boost, 2)
        paper["reasons"] = reasons
        if boost:
            paper["reasons"] = sorted(set(reasons + ["recent"]), key=str.lower)[:12]
    return sorted(papers, key=lambda p: (p.get("score", 0), p.get("published", "")), reverse=True)


def has_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def is_domain_match(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    profile = config.get("research_profile", {})
    required_terms = profile.get("required_core_terms", [])
    if required_terms and not has_any(paper_text(paper), required_terms):
        return False
    venue = paper.get("venue", "").lower()
    if any(journal.lower() in venue for journal in config.get("journals", [])):
        return True
    terms = profile.get("domain_keywords") or profile.get("core_topics") or []
    return has_any(paper_text(paper), terms)


def google_scholar_link(title: str) -> str:
    return "https://scholar.google.com/scholar?" + urllib.parse.urlencode({"q": title})


def compact_list(value: str, sep: str = ",", limit: int = 4) -> str:
    parts = [p.strip() for p in value.split(sep) if p.strip()]
    if not parts:
        return "Unknown"
    if len(parts) <= limit:
        return ", ".join(parts)
    return ", ".join(parts[:limit]) + ", et al."


def score_label(score: float) -> str:
    if score >= 8:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def paper_abstract(paper: dict[str, Any]) -> str:
    abstract = normalize_text(paper.get("abstract"))
    return abstract or "Metadata unavailable"


def truncate_text(text: str, limit: int = 900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def make_markdown(papers: list[dict[str, Any]], config: dict[str, Any], run_date: dt.date) -> str:
    max_papers = int(config.get("max_papers", 8))
    selected = papers[:max_papers]
    detailed = selected[:5]
    remaining = selected[5:]
    high = sum(1 for p in selected if p.get("score", 0) >= 8)
    medium = sum(1 for p in selected if 4 <= p.get("score", 0) < 8)
    language = config.get("language", "en")

    if language.lower().startswith("zh"):
        title = f"# 科研简报 | {run_date.isoformat()}"
        summary = f"今日筛出 {len(selected)} 篇候选论文：高相关 {high} 篇，中等相关 {medium} 篇。"
        priority = "## 今日优先级"
        details = "## 精读清单"
        rest = "## 其余候选"
        advice = "## 今日建议"
        abstract_label = "摘要"
        final_notes = [
            "优先阅读高相关论文；中等相关论文先看摘要、问题定义和实验设置。",
            "如果候选过少，请放宽 query_templates；如果跑题太多，请收紧 domain_keywords。",
            "BibTeX 已同步生成，可导入 Zotero、EndNote 或其他文献管理器。",
        ]
    else:
        title = f"# Research Brief | {run_date.isoformat()}"
        summary = f"Selected {len(selected)} candidate papers: {high} high relevance, {medium} medium relevance."
        priority = "## Today's Priority"
        details = "## Reading List"
        rest = "## Other Candidates"
        advice = "## Suggested Actions"
        abstract_label = "Abstract"
        final_notes = [
            "Read high-relevance papers first; skim medium-relevance papers for problem formulation and experiments.",
            "If there are too few papers, broaden query_templates; if results drift, tighten domain_keywords.",
            "BibTeX files are generated for Zotero, EndNote, or other reference managers.",
        ]

    lines: list[str] = [title, "", summary]
    if selected:
        top = selected[0]
        lines.append(f"Top pick: {top['title']} ({top.get('venue') or 'Unknown'}, {top.get('published') or 'date unknown'}).")
    else:
        lines.append("No relevant papers were found in this run.")
    lines.append("")

    if selected:
        lines.append(priority)
        for idx, paper in enumerate(selected[:3], 1):
            score = float(paper.get("score", 0))
            lines.append(f"{idx}. **{paper['title']}** - {score_label(score)}, {score:.1f}; {paper.get('venue') or 'Unknown'}.")
        lines.append("")

        lines.append(details)
        for idx, paper in enumerate(detailed, 1):
            score = float(paper.get("score", 0))
            reasons = ", ".join(paper.get("reasons", [])) or "semantic match"
            url = paper.get("url") or google_scholar_link(paper["title"])
            lines.append(f"### {idx}. {paper['title']}")
            lines.append(f"- Venue: {paper.get('venue') or 'Unknown'} ({paper.get('source')})")
            lines.append(f"- Authors: {compact_list(paper.get('authors', 'Unknown'))}")
            lines.append(f"- Affiliations: {compact_list(paper.get('affiliations', 'Metadata unavailable'), sep=';', limit=2)}")
            lines.append(f"- Date: {paper.get('published') or 'Unknown'}")
            lines.append(f"- DOI: {paper.get('doi') or 'N/A'}")
            lines.append(f"- Link: {url}")
            lines.append(f"- Scholar: {google_scholar_link(paper['title'])}")
            lines.append(f"- Relevance: {score_label(score)}, {score:.1f}; matched: {reasons}")
            lines.append(f"**{abstract_label}:** {truncate_text(paper_abstract(paper))}")
            lines.append("")

        if remaining:
            lines.append(rest)
            for idx, paper in enumerate(remaining, len(detailed) + 1):
                score = float(paper.get("score", 0))
                url = paper.get("url") or google_scholar_link(paper["title"])
                lines.append(f"- {idx}. {paper['title']} | {paper.get('venue') or 'Unknown'} | {score_label(score)} {score:.1f} | {url}")
            lines.append("")

    lines.append(advice)
    for note in final_notes:
        lines.append(f"- {note}")
    return "\n".join(lines)

def inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)


def markdown_to_html(markdown: str) -> str:
    html_lines = [
        "<html><body style=\"margin:0;background:#f6f7f9;color:#1f2933;font-family:Arial,'Microsoft YaHei',sans-serif;\">",
        "<div style=\"max-width:760px;margin:0 auto;padding:24px 16px;\">",
        "<div style=\"background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:24px;\">",
    ]
    list_stack: list[str] = []

    def close_lists() -> None:
        while list_stack:
            html_lines.append(f"</{list_stack.pop()}>")

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            close_lists()
            html_lines.append("<div style=\"height:8px\"></div>")
            continue
        if line.startswith("# "):
            close_lists()
            html_lines.append(f"<h1 style=\"font-size:24px;line-height:1.3;margin:0 0 12px;color:#111827;\">{inline_html(line[2:])}</h1>")
        elif line.startswith("## "):
            close_lists()
            html_lines.append(f"<h2 style=\"font-size:18px;line-height:1.35;margin:24px 0 10px;color:#0f766e;border-bottom:1px solid #e5e7eb;padding-bottom:6px;\">{inline_html(line[3:])}</h2>")
        elif line.startswith("### "):
            close_lists()
            html_lines.append(f"<h3 style=\"font-size:15px;line-height:1.45;margin:18px 0 8px;color:#111827;\">{inline_html(line[4:])}</h3>")
        elif re.match(r"\d+\. ", line):
            if not list_stack or list_stack[-1] != "ol":
                close_lists()
                list_stack.append("ol")
                html_lines.append("<ol style=\"margin:6px 0 14px 22px;padding:0;\">")
            item = re.sub(r"^\d+\. ", "", line)
            html_lines.append(f"<li style=\"margin:7px 0;line-height:1.55;\">{inline_html(item)}</li>")
        elif line.startswith("- "):
            if not list_stack or list_stack[-1] != "ul":
                close_lists()
                list_stack.append("ul")
                html_lines.append("<ul style=\"margin:6px 0 14px 20px;padding:0;\">")
            html_lines.append(f"<li style=\"margin:6px 0;line-height:1.55;\">{inline_html(line[2:])}</li>")
        else:
            close_lists()
            html_lines.append(f"<p style=\"font-size:14px;line-height:1.7;margin:8px 0;\">{inline_html(line)}</p>")

    close_lists()
    html_lines.append("</div>")
    html_lines.append("<p style=\"font-size:12px;color:#6b7280;margin:12px 4px 0;\">Generated by Research Brief Actions.</p>")
    html_lines.append("</div></body></html>")
    return "\n".join(html_lines)


def bibtex_key(paper: dict[str, Any]) -> str:
    first_author = paper.get("authors", "paper").split(",")[0].split()[-1].lower()
    year = re.search(r"\d{4}", paper.get("published", ""))
    first_title_word = re.sub(r"[^A-Za-z0-9]", "", paper.get("title", "paper").split()[0]).lower()
    return f"{first_author}{year.group(0) if year else 'nd'}{first_title_word}"


def make_bibtex(papers: list[dict[str, Any]], max_papers: int) -> str:
    entries: list[str] = []
    for paper in papers[:max_papers]:
        year_match = re.search(r"\d{4}", paper.get("published", ""))
        fields = {
            "title": paper.get("title", ""),
            "author": paper.get("authors", "").replace(", ", " and "),
            "journal": paper.get("venue", ""),
            "year": year_match.group(0) if year_match else "",
            "doi": paper.get("doi", ""),
            "url": paper.get("url", ""),
        }
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
        entries.append(f"@article{{{bibtex_key(paper)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def email_subject(run_date: dt.date, config: dict[str, Any]) -> str:
    prefix = config.get("email_subject_prefix", "Research Brief")
    return f"{prefix} | {run_date.isoformat()}"


def send_email(markdown: str, config: dict[str, Any], run_date: dt.date) -> str:
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("RESEARCH_BRIEF_FROM_EMAIL")
    if api_key and from_email:
        send_email_sendgrid(markdown, config, run_date, api_key, from_email)
        return "SendGrid"
    send_email_smtp(markdown, config, run_date)
    return "SMTP"


def send_email_sendgrid(markdown: str, config: dict[str, Any], run_date: dt.date, api_key: str, from_email: str) -> None:
    to_email = os.getenv("RESEARCH_BRIEF_TO_EMAIL", config["recipient_email"])
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": email_subject(run_date, config),
        "content": [
            {"type": "text/plain", "value": markdown},
            {"type": "text/html", "value": markdown_to_html(markdown)},
        ],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def send_email_smtp(markdown: str, config: dict[str, Any], run_date: dt.date) -> None:
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if not host or not username or not password:
        raise RuntimeError("Email is not configured. Set either SendGrid secrets or SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD.")

    port = env_int("SMTP_PORT", 587)
    use_ssl = env_bool("SMTP_USE_SSL", port == 465)
    starttls = env_bool("SMTP_STARTTLS", not use_ssl)
    from_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("RESEARCH_BRIEF_FROM_EMAIL") or username
    to_email = os.getenv("SMTP_TO_EMAIL") or os.getenv("RESEARCH_BRIEF_TO_EMAIL", config["recipient_email"])

    message = EmailMessage()
    message["Subject"] = email_subject(run_date, config)
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(markdown)
    message.add_alternative(markdown_to_html(markdown), subtype="html")

    retries = env_int("SMTP_RETRIES", 3)
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    if starttls:
                        server.starttls(context=context)
                    server.login(username, password)
                    server.send_message(message)
            return
        except (OSError, smtplib.SMTPException) as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = 8 * attempt
            print(f"warning: SMTP send attempt {attempt} failed: {exc}; retrying in {wait_seconds}s", file=sys.stderr)
            time.sleep(wait_seconds)
    raise RuntimeError(f"SMTP send failed after {retries} attempts: {last_error}") from last_error


def zotero_library_url() -> tuple[str, str]:
    api_key = os.getenv("ZOTERO_API_KEY")
    user_id = os.getenv("ZOTERO_USER_ID")
    group_id = os.getenv("ZOTERO_GROUP_ID")
    if not api_key:
        raise RuntimeError("ZOTERO_API_KEY must be set to import items to Zotero")
    if group_id:
        return f"https://api.zotero.org/groups/{group_id}", api_key
    if user_id:
        return f"https://api.zotero.org/users/{user_id}", api_key
    raise RuntimeError("ZOTERO_USER_ID or ZOTERO_GROUP_ID must be set to import items to Zotero")


def zotero_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Zotero-API-Key": api_key, "Zotero-API-Version": "3"}


def zotero_creators(authors: str) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for author in [a.strip() for a in authors.split(",") if a.strip()]:
        if author.lower() == "et al.":
            continue
        parts = author.split()
        if len(parts) == 1:
            creators.append({"creatorType": "author", "name": parts[0]})
        else:
            creators.append({"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
    return creators


def zotero_item_from_paper(paper: dict[str, Any], run_date: dt.date) -> dict[str, Any]:
    item = {
        "itemType": "journalArticle",
        "title": paper.get("title", ""),
        "creators": zotero_creators(paper.get("authors", "")),
        "publicationTitle": paper.get("venue", ""),
        "date": paper.get("published", ""),
        "DOI": paper.get("doi", ""),
        "url": paper.get("url", ""),
        "abstractNote": paper.get("abstract", ""),
        "tags": [{"tag": "research-brief"}, {"tag": f"research-brief-{run_date.isoformat()}"}],
    }
    collection_key = os.getenv("ZOTERO_COLLECTION_KEY")
    if collection_key:
        item["collections"] = [collection_key]
    return {k: v for k, v in item.items() if v}


def zotero_item_exists(paper: dict[str, Any], library_url: str, api_key: str) -> bool:
    query = paper.get("doi") or paper.get("title")
    if not query:
        return False
    params = urllib.parse.urlencode({"format": "json", "itemType": "journalArticle", "limit": "5", "q": query, "qmode": "everything"})
    req = urllib.request.Request(f"{library_url}/items?{params}", headers=zotero_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            items = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: failed to check Zotero duplicates for {paper.get('title', 'unknown')}: {exc}", file=sys.stderr)
        return False
    doi = paper.get("doi", "").lower()
    title = title_key(paper.get("title", ""))
    for item in items:
        data = item.get("data", {})
        if doi and normalize_text(data.get("DOI")).lower() == doi:
            return True
        if title and title_key(data.get("title", "")) == title:
            return True
    return False


def import_to_zotero(papers: list[dict[str, Any]], max_papers: int, run_date: dt.date) -> tuple[int, int]:
    library_url, api_key = zotero_library_url()
    imported = 0
    skipped = 0
    for paper in papers[:max_papers]:
        if zotero_item_exists(paper, library_url, api_key):
            skipped += 1
            continue
        req = urllib.request.Request(
            f"{library_url}/items",
            data=json.dumps([zotero_item_from_paper(paper, run_date)]).encode("utf-8"),
            headers=zotero_headers(api_key),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Zotero returned HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        imported += len(payload.get("successful", {}))
    return imported, skipped


def write_outputs(markdown: str, bibtex: str, run_date: dt.date) -> tuple[Path, Path | None]:
    BRIEF_DIR.mkdir(exist_ok=True)
    md_path = BRIEF_DIR / f"{run_date.isoformat()}.md"
    latest_path = BRIEF_DIR / "latest.md"
    md_path.write_text(markdown, encoding="utf-8")
    latest_path.write_text(markdown, encoding="utf-8")
    bib_path = None
    if bibtex:
        bib_path = BRIEF_DIR / f"{run_date.isoformat()}.bib"
        bib_path.write_text(bibtex, encoding="utf-8")
        (BRIEF_DIR / "latest.bib").write_text(bibtex, encoding="utf-8")
    return md_path, bib_path


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=30, help="Search window in days")
    parser.add_argument("--dry-run", action="store_true", help="Do not send email or import to Zotero")
    parser.add_argument("--no-email", action="store_true", help="Do not send email")
    parser.add_argument("--zotero", action="store_true", help="Import selected papers to Zotero via the Zotero Web API")
    args = parser.parse_args()

    config = load_config()
    tz = ZoneInfo(config.get("timezone", "UTC"))
    run_date = dt.datetime.now(tz).date()

    papers: list[dict[str, Any]] = []
    papers.extend(fetch_crossref(config, args.days_back))
    papers.extend(fetch_openalex(config, args.days_back))
    papers.extend(fetch_arxiv(config, args.days_back))
    papers.extend(fetch_ieee(config))
    recent_papers = [paper for paper in dedupe(papers) if is_within_window(paper, args.days_back, run_date)]
    ranked = rank_papers(recent_papers, config, args.days_back, run_date)
    relevant = [p for p in ranked if p.get("score", 0) > 0 and is_domain_match(p, config)]
    relevant = remove_seen_papers(relevant, load_seen_papers())
    ranked = [p for p in relevant if p.get("score", 0) >= 4] or relevant

    markdown = make_markdown(ranked, config, run_date)
    bibtex = make_bibtex(ranked, int(config.get("max_papers", 8))) if config.get("generate_bibtex") else ""
    md_path, bib_path = write_outputs(markdown, bibtex, run_date)
    print(f"wrote {display_path(md_path)}")
    if bib_path:
        print(f"wrote {display_path(bib_path)}")

    if not args.dry_run and not args.no_email:
        provider = send_email(markdown, config, run_date)
        print(f"sent email via {provider}")
    else:
        print("email sending skipped")

    if args.zotero and not args.dry_run:
        imported, skipped = import_to_zotero(ranked, int(config.get("max_papers", 8)), run_date)
        print(f"zotero import complete: imported {imported}, skipped existing {skipped}")
    elif args.zotero:
        print("zotero import skipped")
    if not args.dry_run:
        update_seen_papers(ranked)
        print(f"updated {display_path(seen_papers_path())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
