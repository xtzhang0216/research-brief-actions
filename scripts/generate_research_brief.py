
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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "automation" / "research_brief_config.json"
BRIEF_DIR = ROOT / "research_briefs"
APP_NAME = "research-brief-actions/1.0"


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


def date_from_parts(parts: Any) -> str:
    try:
        date_parts = parts["date-parts"][0]
        year = int(date_parts[0])
        month = int(date_parts[1]) if len(date_parts) > 1 else 1
        day = int(date_parts[2]) if len(date_parts) > 2 else 1
        return dt.date(year, month, day).isoformat()
    except Exception:
        return ""


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:160]


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
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", [])) + list(config.get("journals", []))
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
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_back)).isoformat()
    queries = list(config.get("query_templates", [])) + [f'"{j}"' for j in config.get("journals", [])]
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


def dedupe(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for paper in papers:
        key = paper.get("doi") or title_key(paper.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(paper)
    return out


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


def rank_papers(papers: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    for paper in papers:
        score, reasons = score_paper(paper, config)
        paper["score"] = score
        paper["reasons"] = reasons
    return sorted(papers, key=lambda p: (p.get("score", 0), p.get("published", "")), reverse=True)


def has_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def is_domain_match(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    profile = config.get("research_profile", {})
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


def focus_note(paper: dict[str, Any], config: dict[str, Any]) -> str:
    text = paper_text(paper)
    profile = config.get("research_profile", {})
    notes: list[str] = []
    for keyword in profile.get("core_topics", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"matches {keyword}")
    for keyword in profile.get("method_keywords", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"uses or mentions {keyword}")
    for keyword in profile.get("objective_keywords", [])[:8]:
        if keyword.lower() in text:
            notes.append(f"touches {keyword}")
    if not notes:
        notes.append("overlaps with your profile through title, venue, or concepts")
    return "; ".join(notes[:3]) + "."


def action_note(score: float) -> str:
    if score >= 8:
        return "Read today: inspect the problem formulation, assumptions, and evaluation setup."
    if score >= 4:
        return "Save and skim: check abstract, system model, and baselines before deep reading."
    return "Low priority: keep only if the title directly supports current writing."


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
        why = "为什么值得看"
        action = "建议动作"
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
        why = "Why it matters"
        action = "Action"
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
            lines.append(f"**{why}:** {focus_note(paper, config)}")
            lines.append(f"**{action}:** {action_note(score)}")
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


def send_email_smtp(markdown: str, config: dict[str, Any], run_date: dt.date) -> None:
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if not host or not username or not password:
        raise RuntimeError("Email is not configured. Set either SendGrid secrets or SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD.")

    port = int(os.getenv("SMTP_PORT", "587"))
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

    retries = int(os.getenv("SMTP_RETRIES", "3"))
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=7, help="Search window in days")
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
    papers.extend(fetch_ieee(config))
    ranked = rank_papers(dedupe(papers), config)
    relevant = [p for p in ranked if p.get("score", 0) > 0 and is_domain_match(p, config)]
    ranked = [p for p in relevant if p.get("score", 0) >= 4] or relevant or [p for p in ranked if p.get("score", 0) > 0]

    markdown = make_markdown(ranked, config, run_date)
    bibtex = make_bibtex(ranked, int(config.get("max_papers", 8))) if config.get("generate_bibtex") else ""
    md_path, bib_path = write_outputs(markdown, bibtex, run_date)
    print(f"wrote {md_path.relative_to(ROOT)}")
    if bib_path:
        print(f"wrote {bib_path.relative_to(ROOT)}")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

