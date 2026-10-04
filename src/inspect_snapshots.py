"""Build step 1: inspect sample captures from each source/era and report their structure.

Writes docs/inspection_report.md. Re-runnable; only reads the local cache.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pandas as pd
from bs4 import BeautifulSoup

from .common import RAW, ROOT, read_gz_text, setup_logging
from .fetch_wayback import MANIFEST

REMIX = re.compile(r"window\.__remixContext\s*=\s*(\{.*?\});\s*</script>", re.S)


def describe(kind: str, text: str) -> dict:
    out: dict = {"bytes": len(text)}
    if kind == "api":
        d = json.loads(text)
        jobs = d.get("jobs", [])
        out.update(format="api_json", jobs_listed=len(jobs), total=(d.get("meta") or {}).get("total"),
                   has_content=bool(jobs and "content" in jobs[0]),
                   has_departments=bool(jobs and "departments" in jobs[0]))
        return out
    if m := REMIX.search(text):
        ld = json.loads(m.group(1))["state"]["loaderData"]
        route = next((v for k, v in ld.items() if k != "root" and isinstance(v, dict)), {})
        if "jobPosts" in route:
            jp = route["jobPosts"]
            out.update(format="remix_board", jobs_listed=len(jp.get("data", [])), total=jp.get("total"),
                       page=jp.get("page"), total_pages=jp.get("total_pages"),
                       fields=",".join(sorted(jp["data"][0].keys())) if jp.get("data") else "")
        elif "jobPost" in route:
            p = route["jobPost"]
            out.update(format="remix_job", has_content=bool(p.get("content")), published_at=p.get("published_at"),
                       has_pay=bool(p.get("pay_ranges")), has_department="department" in p)
        else:
            out.update(format="remix_other")
        return out
    soup = BeautifulSoup(text, "lxml")
    if openings := soup.select("div.opening"):
        out.update(format="legacy_board", jobs_listed=len(openings),
                   sections=len(soup.select("section.level-0")),
                   pagination=bool(soup.select("a[href*='page=']")))
    elif soup.select_one(".app-title"):
        ld = soup.select_one("script[type='application/ld+json']")
        out.update(format="legacy_job", has_content=bool(soup.select_one("#content")),
                   date_posted=(json.loads(ld.string).get("datePosted") if ld and ld.string else None))
    else:
        title = soup.title.get_text(strip=True) if soup.title else ""
        out.update(format="unknown", title=title[:80])
    return out


def main() -> None:
    setup_logging()
    m = pd.read_csv(MANIFEST, dtype={"timestamp": str, "job_id": str})
    have = m[m["download"] & m["dup_of"].isna() & m["path"].map(lambda p: (RAW / p).exists())].copy()
    have["quarter"] = pd.to_datetime(have["timestamp"].str[:8]).dt.to_period("Q").astype(str)

    # Up to 2 samples per source/kind/quarter (spread across the period).
    samples = pd.concat([g.iloc[[0, len(g) // 2]].drop_duplicates("path")
                         for _, g in have.groupby(["source", "kind", "quarter"])])
    rows = []
    for _, r in samples.iterrows():
        try:
            info = describe(r["kind"], read_gz_text(RAW / r["path"]))
        except Exception as e:
            info = {"format": f"error: {type(e).__name__}"}
        rows.append({"source": r["source"], "kind": r["kind"], "quarter": r["quarter"],
                     "timestamp": r["timestamp"], "page": r["page"], **info})
    s = pd.DataFrame(rows)

    # Full scan of board pages: listed vs total → how much page 1 under-counts.
    boards = have[have["kind"] == "board"]
    full = []
    for _, r in boards.iterrows():
        try:
            info = describe("board", read_gz_text(RAW / r["path"]))
        except Exception as e:
            info = {"format": f"error: {type(e).__name__}"}
        full.append({"source": r["source"], "timestamp": r["timestamp"], "page": r["page"], **info})
    b = pd.DataFrame(full)

    lines = ["# Snapshot inspection report", "",
             f"Captures cached: {len(have)} of {int((m['download'] & m['dup_of'].isna()).sum())} planned.", "",
             "## Formats by source/kind", "", "```",
             s.groupby(["source", "kind"])["format"].agg(lambda x: dict(Counter(x))).to_string(), "```", "",
             "## Samples", "", s.drop(columns=["fields"], errors="ignore").to_markdown(index=False), ""]
    if "total" in b:
        b["listed_share"] = b["jobs_listed"] / b["total"]
        lines += ["## Board pages: jobs listed vs board total", "",
                  "`total` is the board's own job count; page 1 lists at most 50.", "", "```",
                  b.groupby(["source", "format"]).agg(n=("timestamp", "size"),
                                                      median_listed=("jobs_listed", "median"),
                                                      median_total=("total", "median"),
                                                      median_listed_share=("listed_share", "median")).to_string(),
                  "```", ""]
    out = ROOT / "docs" / "inspection_report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines))
    print(out)


if __name__ == "__main__":
    main()
