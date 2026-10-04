"""Stage 1–2: discover Wayback captures (CDX) and download them into a local cache.

Every host in settings.sources is listed with matchType=prefix, then each capture URL is
classified as:

  board      the job board listing (page 1 unless ?page=N); query noise such as
             ?gh_src=… or ?error=true still renders the full page-1 board
  board_filtered  ?departments[]=… / ?offices[]=… filtered views (recorded, not downloaded)
  job        a single job detail page /anthropic/jobs/<id>
  api        the boards-api JSON listing (incl. ?content=true)

Downloads are cached under data/raw/wayback/ and never repeated. Captures whose CDX digest
matches an earlier capture are recorded but point at the first file (`dup_of`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import requests

from .common import PROCESSED, RAW, log, settings, setup_logging, write_gz

WAYBACK = RAW / "wayback"
CDX_DIR = WAYBACK / "cdx"
MANIFEST = WAYBACK / "manifest.csv"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
CDX_FIELDS = ["timestamp", "original", "statuscode", "mimetype", "digest"]

JOB_PATH = re.compile(r"^/anthropic/jobs/(\d+)/?$")
BOARD_PATH = re.compile(r"^/anthropic/?$")
API_PATH = re.compile(r"^/v1/boards/anthropic/jobs/?$")


class Fetcher:
    """Single-threaded, rate-limited HTTP client with exponential backoff."""

    def __init__(self) -> None:
        cfg = settings()
        self.interval = float(cfg["request_interval_seconds"])
        self.max_retries = int(cfg["max_retries"])
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg["user_agent"]
        self._last = 0.0

    def get(self, url: str, params: dict | None = None, timeout: int = 120) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=timeout)
                if r.status_code == 200:
                    return r
                if r.status_code not in (429, 500, 502, 503, 504, 520, 522, 524):
                    r.raise_for_status()
                reason = f"HTTP {r.status_code}"
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = type(e).__name__
            if attempt == self.max_retries:
                raise RuntimeError(f"giving up after {attempt + 1} attempts ({reason}): {url}")
            backoff = min(2 ** (attempt + 1), 60)
            log.warning("%s, retrying in %ss: %s", reason, backoff, url)
            time.sleep(backoff)
        raise AssertionError("unreachable")


def classify(source: str, original: str) -> tuple[str | None, str | None, int | None]:
    """Return (kind, job_id, page) for a capture URL, or (None, …) to ignore it."""
    parts = urlsplit(original)
    path = parts.path.replace("%5C", "\\")
    qs = parse_qs(parts.query)
    if source == "api":
        return ("api", None, None) if API_PATH.match(path) else (None, None, None)
    if m := JOB_PATH.match(path):
        return "job", m.group(1), None
    if BOARD_PATH.match(path):
        if any(k.startswith(("departments", "offices", "q")) for k in qs):
            return "board_filtered", None, None
        page = qs.get("page", ["1"])[0]
        return "board", None, int(page) if page.isdigit() else 1
    return None, None, None


def discover(fetcher: Fetcher, refresh: bool = False) -> pd.DataFrame:
    """List all 200-status captures per source (cached CDX responses)."""
    cfg = settings()
    since = str(cfg["backfill_from"]).replace("-", "")
    rows = []
    for src in cfg["sources"]:
        cache = CDX_DIR / f"{src['name']}.json"
        if refresh or not cache.exists():
            log.info("CDX listing %s", src["host"])
            r = fetcher.get(CDX_ENDPOINT, params={
                "url": src["host"], "matchType": "prefix", "output": "json",
                "fl": ",".join(CDX_FIELDS), "filter": "statuscode:200", "from": since,
            }, timeout=300)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(r.text)
        listing = json.loads(cache.read_text() or "[]")
        for rec in listing[1:]:
            row = dict(zip(CDX_FIELDS, rec))
            kind, job_id, page = classify(src["name"], row["original"])
            if kind:
                rows.append({"source": src["name"], "kind": kind, "job_id": job_id, "page": page, **row})
    df = pd.DataFrame(rows).drop_duplicates(["source", "timestamp", "original"])
    df["page"] = df["page"].astype("Int64")
    return df.sort_values(["source", "kind", "timestamp"]).reset_index(drop=True)


def raw_path(row) -> Path:
    ext = "json" if row["kind"] == "api" else "html"
    h = hashlib.sha1(row["original"].encode()).hexdigest()[:8]
    return WAYBACK / row["source"] / row["kind"] / f"{row['timestamp']}_{h}.{ext}.gz"


def plan_downloads(df: pd.DataFrame) -> pd.DataFrame:
    """Mark which captures to download; job pages only at the configured captures per job."""
    wanted = set(settings()["job_page_captures"])
    df = df.copy()
    df["download"] = df["kind"].isin(["board", "api"])
    jobs = df[df["kind"] == "job"]
    for _, g in jobs.groupby(["source", "job_id"]):
        if "earliest" in wanted:
            df.loc[g["timestamp"].idxmin(), "download"] = True
        if "latest" in wanted:
            df.loc[g["timestamp"].idxmax(), "download"] = True
    # Identical bytes (same digest) are fetched once; later captures reference the first file.
    df["path"] = [str(raw_path(r).relative_to(RAW)) for _, r in df.iterrows()]
    df["dup_of"] = None
    first_by_digest: dict[str, str] = {}
    for i in df.index[df["download"]].tolist():
        d = df.at[i, "digest"]
        if d in first_by_digest:
            df.at[i, "dup_of"] = first_by_digest[d]
        else:
            first_by_digest[d] = df.at[i, "path"]
    return df


def download(fetcher: Fetcher, df: pd.DataFrame, limit: int | None = None) -> pd.DataFrame:
    todo = df[df["download"] & df["dup_of"].isna()]
    missing = [i for i in todo.index if not (RAW / df.at[i, "path"]).exists()]
    log.info("%d captures planned, %d cached, %d to download", len(todo), len(todo) - len(missing), len(missing))
    if limit is not None:
        missing = missing[:limit]
    df["fetch_status"] = ["cached" if (RAW / p).exists() else "" for p in df["path"]]
    for n, i in enumerate(missing, 1):
        row = df.loc[i]
        url = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        try:
            r = fetcher.get(url)
            write_gz(RAW / row["path"], r.content)
            df.at[i, "fetch_status"] = "ok"
        except Exception as e:  # degrade gracefully; next run retries
            log.warning("fetch failed (%s): %s", e.__class__.__name__, url)
            df.at[i, "fetch_status"] = "error"
        if n % 50 == 0:
            log.info("downloaded %d/%d", n, len(missing))
            save_manifest(df)
    return df


def save_manifest(df: pd.DataFrame) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(MANIFEST, index=False)


def write_coverage(df: pd.DataFrame) -> pd.DataFrame:
    """coverage.csv: captures per month per source/kind, plus board-observation gaps > N days."""
    gap_days = int(settings()["gap_days"])
    d = df[df["kind"].isin(["board", "api", "job"])].copy()
    d["ts"] = pd.to_datetime(d["timestamp"], format="%Y%m%d%H%M%S")
    d["month"] = d["ts"].dt.strftime("%Y-%m")
    d["day"] = d["ts"].dt.date
    monthly = (d.groupby(["month", "source", "kind"])
                .agg(n_captures=("timestamp", "size"), n_days=("day", "nunique"))
                .reset_index())
    monthly.insert(0, "row_type", "month")

    gaps = []
    boards = d[d["kind"].isin(["board", "api"])]
    for label, g in [("all_boards", boards), *boards.groupby("source")]:
        days = sorted(set(g["day"]))
        for a, b in zip(days, days[1:]):
            if (b - a).days > gap_days:
                gaps.append({"row_type": "gap", "source": label, "gap_start": a, "gap_end": b, "gap_days": (b - a).days})
    out = pd.concat([monthly, pd.DataFrame(gaps)], ignore_index=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    out.to_csv(PROCESSED / "coverage.csv", index=False)
    return out


def run(refresh_cdx: bool = False, limit: int | None = None, discover_only: bool = False) -> pd.DataFrame:
    fetcher = Fetcher()
    df = plan_downloads(discover(fetcher, refresh=refresh_cdx))
    summary = df.groupby(["source", "kind"]).agg(captures=("timestamp", "size"), jobs=("job_id", "nunique"),
                                                  to_download=("download", "sum"))
    log.info("discovered captures:\n%s", summary.to_string())
    write_coverage(df)
    if not discover_only:
        df = download(fetcher, df, limit=limit)
    else:
        df["fetch_status"] = ["cached" if (RAW / p).exists() else "" for p in df["path"]]
    save_manifest(df)
    return df


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--refresh-cdx", action="store_true", help="re-query CDX instead of using cached listings")
    p.add_argument("--discover-only", action="store_true")
    p.add_argument("--limit", type=int, help="download at most N new captures (for testing)")
    a = p.parse_args()
    setup_logging()
    t0 = datetime.now()
    run(a.refresh_cdx, a.limit, a.discover_only)
    log.info("done in %s", datetime.now() - t0)


if __name__ == "__main__":
    main()
