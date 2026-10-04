"""Local preview of every cached job page: python -m src.dump_descriptions

Writes data/raw/descriptions_preview.html (gitignored; descriptions are never published).
One entry per job ID, from its latest cached capture.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .common import RAW, read_gz_text, setup_logging

REMIX = re.compile(r"window\.__remixContext\s*=\s*(\{.*?\});\s*</script>", re.S)
OUT = RAW / "descriptions_preview.html"


def clean_html(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "lxml")
    for tag in soup(["script", "style", "iframe", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k == "href"}
    body = soup.body
    return body.decode_contents() if body else ""


def extract(path: Path) -> dict | None:
    t = read_gz_text(path)
    if m := REMIX.search(t):
        ld = json.loads(m.group(1))["state"]["loaderData"]
        p = next((v["jobPost"] for v in ld.values() if isinstance(v, dict) and "jobPost" in v), None)
        if not p:
            return None
        pay = "; ".join(f"{r.get('min')}–{r.get('max')} {r.get('currency_type', '')}" for r in p.get("pay_ranges") or [])
        return {"title": p.get("title"), "location": p.get("job_post_location"), "published": p.get("published_at"),
                "pay": pay, "intro": p.get("introduction"), "content": p.get("content"), "conclusion": p.get("conclusion")}
    s = BeautifulSoup(t, "lxml")
    title = s.select_one(".app-title")
    if not title:
        return None
    ld = s.select_one("script[type='application/ld+json']")
    published = None
    if ld and ld.string:
        try:
            published = json.loads(ld.string).get("datePosted")
        except ValueError:
            pass
    loc = s.select_one(".location")
    content = s.select_one("#content")
    return {"title": title.get_text(strip=True), "location": loc.get_text(strip=True) if loc else None,
            "published": published, "pay": "", "intro": None,
            "content": content.decode_contents() if content else "", "conclusion": None}


def main() -> None:
    setup_logging()
    latest: dict[str, Path] = {}
    for f in sorted(RAW.glob("wayback/*/job/*.html.gz")):  # sorted → later timestamps overwrite
        m = re.search(r"/jobs/(\d+)", read_gz_text(f)[:200000])
        latest[m.group(1) if m else f.name] = f

    entries, failed = [], 0
    for job_id, f in latest.items():
        try:
            d = extract(f)
        except Exception:
            d = None
        if not d:
            failed += 1
            continue
        entries.append((d.get("published") or "", job_id, f, d))
    entries.sort(key=lambda e: e[0], reverse=True)

    parts = []
    for published, job_id, f, d in entries:
        extra = "".join(
            f"<details><summary>{label}</summary>{clean_html(d[key])}</details>"
            for key, label in [("intro", "Introduction (shared boilerplate)"), ("conclusion", "Conclusion (shared boilerplate)")]
            if d.get(key))
        parts.append(
            f"<article id='{job_id}'><h2>{html.escape(d['title'] or '')}</h2>"
            f"<p class='meta'>ID {job_id} · {html.escape(d['location'] or '—')} · published {html.escape(published or '—')}"
            f"{' · ' + html.escape(d['pay']) if d['pay'] else ''}<br><code>{f.relative_to(RAW)}</code></p>"
            f"{clean_html(d['content'])}{extra}</article>")

    OUT.write_text(f"""<!doctype html><meta charset="utf-8"><title>Job descriptions (local, private)</title>
<style>body{{font:15px/1.5 system-ui;max-width:860px;margin:2rem auto;padding:0 16px}}
article{{border-top:2px solid #ccc;padding:1rem 0}} .meta{{color:#666;font-size:13px}}
#q{{position:sticky;top:0;width:100%;padding:8px;font-size:15px}}</style>
<input id="q" placeholder="Filter by any text… ({len(entries)} jobs)">
<p>{len(entries)} jobs from latest cached capture, newest published first. {failed} pages could not be parsed.</p>
{''.join(parts)}
<script>q.oninput=()=>{{const s=q.value.toLowerCase();document.querySelectorAll('article').forEach(a=>a.hidden=s&&!a.textContent.toLowerCase().includes(s))}}</script>""")
    print(f"{OUT}  ({len(entries)} jobs, {failed} unparsed)")


if __name__ == "__main__":
    main()
