"""Parsers for archived Greenhouse HTML, one function per markup era.

- remix (job-boards.greenhouse.io, Mar 2025 →): server-rendered Remix app. All data sits in the
  embedded `window.__remixContext` JSON. Board pages list 50 jobs per page (`jobPosts.page`,
  `total`, `total_pages`); job pages hold one `jobPost`.
- legacy (boards.greenhouse.io, → Mar 2025): plain HTML. The board lists every opening on one
  page as `div.opening` rows grouped under department `h3`s; job pages use `.app-title` etc.

Pages are recognised by content, not URL: a job URL can serve the board (e.g. after a job closes).
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .common import ParsedPage, job_id_from_url, money, row, salary_from_text

REMIX = re.compile(r"window\.__remixContext\s*=\s*(\{.*?\});\s*</script>", re.S)


def parse_html(text: str, url: str | None = None) -> ParsedPage:
    if m := REMIX.search(text):
        return _parse_remix(json.loads(m.group(1)), url)
    soup = BeautifulSoup(text, "lxml")
    if soup.select("div.opening"):
        return _parse_legacy_board(soup)
    if soup.select_one(".app-title"):
        return _parse_legacy_job(soup, url)
    return ParsedPage("unknown")


# --- remix era -----------------------------------------------------------------------------

def _parse_remix(ctx: dict, url: str | None) -> ParsedPage:
    loader = ctx.get("state", {}).get("loaderData", {})
    route = next((v for k, v in loader.items() if k != "root" and isinstance(v, dict)), {})
    if "jobPosts" in route:
        return _parse_remix_board(route)
    if "jobPost" in route:
        return _parse_remix_job(route, url)
    return ParsedPage("remix_other")


def _remix_posts(block) -> list[dict]:
    if isinstance(block, dict):
        return block.get("data") or []
    return block or []


def _parse_remix_board(route: dict) -> ParsedPage:
    jp = route["jobPosts"]
    page = ParsedPage("remix_board", page=jp.get("page"), total=jp.get("total"),
                      total_pages=jp.get("total_pages"), is_listing=True)
    seen = set()
    # Featured posts are shown above the list; they are also regular open jobs.
    for p in _remix_posts(jp) + _remix_posts(route.get("featuredPosts")):
        jid = p.get("id") or job_id_from_url(p.get("absolute_url"))
        if jid is None or str(jid) in seen:
            continue
        seen.add(str(jid))
        page.rows.append(row(
            job_id=jid, title=p.get("title"), team=(p.get("department") or {}).get("name"),
            location=p.get("location"), url=p.get("absolute_url"), published_at=p.get("published_at"),
            internal_job_id=p.get("internal_job_id"), requisition_id=p.get("requisition_id"),
        ))
    return page


def _parse_remix_job(route: dict, url: str | None) -> ParsedPage:
    p = route.get("jobPost")
    if not p:  # job page shell without a post (closed / removed)
        return ParsedPage("remix_job_empty")
    jid = route.get("jobPostId") or job_id_from_url(p.get("public_url")) or job_id_from_url(url)
    page = ParsedPage("remix_job")
    page.rows.append(row(job_id=jid, title=p.get("title"), location=p.get("job_post_location"),
                         url=p.get("public_url"), published_at=p.get("published_at")))
    ranges = p.get("pay_ranges") or []
    mins = [v for r in ranges if (v := money(r.get("min"))) is not None]
    maxs = [v for r in ranges if (v := money(r.get("max"))) is not None]
    curs = {r.get("currency_type") for r in ranges if r.get("currency_type")}
    body = "\n".join(x for x in (p.get("introduction"), p.get("content"), p.get("conclusion")) if x)
    page.detail.append({"job_id": str(jid), "description_html": body or None,
                        "salary_min": min(mins) if mins else None, "salary_max": max(maxs) if maxs else None,
                        "currency": curs.pop() if len(curs) == 1 else None})
    return page


# --- legacy era ----------------------------------------------------------------------------

def _parse_legacy_board(soup: BeautifulSoup) -> ParsedPage:
    depts = {h.get("id"): h.get_text(" ", strip=True) for h in soup.select("section h2[id], section h3[id], section h4[id]")}
    page = ParsedPage("legacy_board", page=1, total_pages=1, is_listing=True)
    seen = set()
    for o in soup.select("div.opening"):
        a = o.find("a", href=True)
        if not a:
            continue
        jid = job_id_from_url(a["href"])
        key = jid or a.get_text(strip=True)
        if key in seen:
            continue
        seen.add(key)
        loc = o.select_one(".location")
        page.rows.append(row(job_id=jid, title=a.get_text(" ", strip=True),
                             team=depts.get(o.get("department_id")),
                             location=loc.get_text(" ", strip=True) if loc else None,
                             url=a["href"]))
    page.total = len(page.rows)
    return page


def _parse_legacy_job(soup: BeautifulSoup, url: str | None) -> ParsedPage:
    canon = soup.select_one('meta[property="og:url"]') or soup.select_one("link[rel=canonical]")
    canon_url = canon.get("content") or canon.get("href") if canon else None
    jid = job_id_from_url(canon_url) or job_id_from_url(url)
    posted = None
    if (ld := soup.select_one('script[type="application/ld+json"]')) and ld.string:
        try:
            posted = json.loads(ld.string).get("datePosted")
        except json.JSONDecodeError:
            pass
    loc = soup.select_one(".location")
    page = ParsedPage("legacy_job")
    page.rows.append(row(job_id=jid, title=soup.select_one(".app-title").get_text(" ", strip=True),
                         location=loc.get_text(" ", strip=True) if loc else None,
                         url=canon_url or url, published_at=posted))
    content = soup.select_one("#content")
    pay = soup.select_one(".pay-range")
    lo, hi, cur = salary_from_text(pay.get_text(" ", strip=True) if pay else None)
    page.detail.append({"job_id": jid, "description_html": content.decode_contents() if content else None,
                        "salary_min": lo, "salary_max": hi, "currency": cur})
    return page
