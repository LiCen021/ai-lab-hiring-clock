"""Stage 3: parse every cached capture once, emit job sightings, and register listings.

A *capture* is one Wayback timestamp of one URL. Board pages and API responses are *listing*
captures (a list of open jobs); job pages are single-job captures.

The current board shows 50 jobs per page, and the archive mostly holds page 1. Board captures
taken within LISTING_WINDOW of each other are stitched into one *listing*; a listing is
`complete` when every page 1..total_pages is present and the stitched job count matches the
board's own total. Only complete, non-suspect listings count as evidence that a job is absent.

Outputs
  data/processed/snapshots.csv     one row per listing capture (public)
  data/processed/listings.csv      one row per stitched listing (public)
  data/processed/sightings.parquet job_id × capture, long format (public)
  data/raw/derived/job_rows.parquet    every parsed job row with attributes (local cache)
  data/raw/derived/details.parquet     description HTML + salary per job capture (PRIVATE)
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .common import PROCESSED, RAW, log, read_gz_text, settings
from .fetch_wayback import MANIFEST
from .parse.common import DETAIL_FIELDS, ROW_FIELDS, ParsedPage
from .parse.greenhouse_api import parse_api
from .parse.greenhouse_html import parse_html

DERIVED = RAW / "derived"
PER_PAGE = 50                         # job-boards.greenhouse.io page size
LISTING_WINDOW = timedelta(hours=48)  # max span of captures stitched into one listing
ROLLING_NEIGHBOURS = 5


def parse_file(path: str, kind: str, url: str) -> ParsedPage:
    text = read_gz_text(RAW / path)
    return parse_api(text) if kind == "api" else parse_html(text, url)


def load_captures() -> pd.DataFrame:
    m = pd.read_csv(MANIFEST, dtype={"timestamp": str, "job_id": str, "digest": str})
    m = m[m["download"]].copy()
    m["file"] = m["dup_of"].fillna(m["path"])
    m = m[m["file"].map(lambda p: (RAW / p).exists())]
    m["observed_at"] = pd.to_datetime(m["timestamp"], format="%Y%m%d%H%M%S", utc=True)
    m = m.sort_values(["observed_at", "original"]).reset_index(drop=True)
    m["cap_id"] = m.index
    return m


def parse_all(caps: pd.DataFrame):
    cache: dict[str, ParsedPage] = {}
    snaps, rows, details = [], [], []
    for n, c in enumerate(caps.itertuples(index=False), 1):
        if c.file not in cache:
            try:
                cache[c.file] = parse_file(c.file, c.kind, c.original)
            except Exception as e:
                log.warning("parse error %s: %s", type(e).__name__, c.file)
                cache[c.file] = ParsedPage("parse_error")
            if n % 500 == 0:
                log.info("parsed %d/%d captures", n, len(caps))
        p = cache[c.file]
        base = {"cap_id": c.cap_id, "observed_at": c.observed_at, "source": c.source}
        capture_kind = "job" if not p.is_listing else ("api" if c.kind == "api" else "board")
        if p.is_listing or (p.format in ("parse_error", "unknown") and c.kind != "job"):
            snaps.append({**base, "original_url": c.original, "kind": c.kind if not p.is_listing else capture_kind,
                          "format": p.format,
                          "page": p.page or (1 if p.is_listing else None), "job_count": len(p.rows),
                          "total": p.total, "total_pages": p.total_pages,
                          "duplicate_digest": isinstance(c.dup_of, str)})
        for r in p.rows:
            rows.append({**base, "kind": capture_kind, "page": p.page, **r})
        for d in p.detail:
            details.append({**base, "raw_path": c.path, **d})
    fmt = pd.Series({k: v.format for k, v in cache.items()}).value_counts()
    log.info("parsed %d files: %s", len(cache), fmt.to_dict())
    return (pd.DataFrame(snaps),
            pd.DataFrame(rows, columns=["cap_id", "observed_at", "source", "kind", "page", *ROW_FIELDS]),
            pd.DataFrame(details, columns=["cap_id", "observed_at", "source", "raw_path", *DETAIL_FIELDS]))


def assign_listings(snaps: pd.DataFrame) -> pd.DataFrame:
    """Give each listing capture a listing_id; stitch paginated board pages within the window."""
    snaps = snaps.sort_values("observed_at").reset_index(drop=True)
    snaps["listing_id"] = None
    ok = snaps["format"].isin(["remix_board", "legacy_board", "api_json"])
    for i in snaps.index[ok & (snaps["format"] != "remix_board")]:
        snaps.at[i, "listing_id"] = f"{snaps.at[i, 'source']}:{snaps.at[i, 'observed_at']:%Y%m%dT%H%M%S}"
    start = None
    for i in snaps.index[snaps["format"] == "remix_board"]:
        t = snaps.at[i, "observed_at"]
        if start is None or t - start > LISTING_WINDOW:
            start = t
        snaps.at[i, "listing_id"] = f"job_boards:{start:%Y%m%dT%H%M%S}"
    return snaps


def flag_page_suspects(snaps: pd.DataFrame) -> pd.DataFrame:
    """A board page is suspect if it lists < threshold × the jobs its own counts imply."""
    thr = float(settings()["suspect_threshold"])
    snaps["status"] = "ok"
    snaps.loc[snaps["format"].isin(["parse_error", "unknown"]), "status"] = "parse_error"
    remix = snaps["format"] == "remix_board"
    expected = (snaps["total"] - (snaps["page"] - 1) * PER_PAGE).clip(upper=PER_PAGE)
    snaps.loc[remix & (snaps["job_count"] < thr * expected), "status"] = "suspect"
    return snaps


def build_listings(snaps: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    valid = snaps[snaps["listing_id"].notna() & (snaps["status"] == "ok")]
    key = rows.merge(valid[["cap_id", "listing_id"]], on="cap_id")
    jobs_per_listing = key.groupby("listing_id")["job_id"].nunique()
    out = []
    for lid, g in valid.groupby("listing_id"):
        pages = sorted({int(p) for p in g["page"].dropna()})
        total_pages = int(g["total_pages"].max()) if g["total_pages"].notna().any() else 1
        total = int(g["total"].max()) if g["total"].notna().any() else None
        n = int(jobs_per_listing.get(lid, 0))
        pages_ok = set(range(1, total_pages + 1)) <= set(pages)
        count_ok = total is None or n >= total - max(2, round(0.02 * total))
        out.append({"listing_id": lid, "source": g["source"].iloc[0],
                    "start": g["observed_at"].min(), "end": g["observed_at"].max(),
                    "n_captures": len(g), "pages_present": ",".join(map(str, pages)),
                    "total_pages": total_pages, "board_total": total, "job_count": n,
                    "complete": bool(pages_ok and count_ok)})
    listings = pd.DataFrame(out).sort_values("end").reset_index(drop=True)

    # Rolling-median check across complete listings (all sources, chronological).
    thr = float(settings()["suspect_threshold"])
    listings["status"] = "ok"
    comp = listings.index[listings["complete"]]
    counts = listings.loc[comp, "job_count"].tolist()
    k = ROLLING_NEIGHBOURS // 2
    for pos, i in enumerate(comp):
        neigh = counts[max(0, pos - k):pos] + counts[pos + 1:pos + 1 + k]
        if neigh and counts[pos] < thr * pd.Series(neigh).median():
            listings.at[i, "status"] = "suspect"
    return listings


def run() -> dict[str, pd.DataFrame]:
    caps = load_captures()
    log.info("%d captures with cached files (%d unique files)", len(caps), caps["file"].nunique())
    snaps, rows, details = parse_all(caps)
    snaps = flag_page_suspects(assign_listings(snaps))
    listings = build_listings(snaps, rows)
    snaps = snaps.merge(listings[["listing_id", "complete", "status"]].rename(
        columns={"complete": "listing_complete", "status": "listing_status"}), on="listing_id", how="left")

    rows = rows.merge(snaps[["cap_id", "listing_id"]], on="cap_id", how="left")

    PROCESSED.mkdir(parents=True, exist_ok=True)
    DERIVED.mkdir(parents=True, exist_ok=True)
    snap_cols = ["observed_at", "source", "original_url", "job_count", "status", "kind", "format", "page",
                 "total", "total_pages", "duplicate_digest", "listing_id", "listing_complete", "listing_status"]
    snaps[snap_cols].to_csv(PROCESSED / "snapshots.csv", index=False)
    listings.to_csv(PROCESSED / "listings.csv", index=False)
    rows[["job_id", "observed_at", "source", "kind", "page"]].to_parquet(PROCESSED / "sightings.parquet", index=False)
    rows.to_parquet(DERIVED / "job_rows.parquet", index=False)
    details.to_parquet(DERIVED / "details.parquet", index=False)

    log.info("snapshots: %d listing captures (%s); listings: %d (%d complete, %d suspect)",
             len(snaps), snaps["status"].value_counts().to_dict(), len(listings),
             int(listings["complete"].sum()), int((listings["status"] == "suspect").sum()))
    log.info("job rows: %d parsed, %d distinct job IDs", len(rows), rows["job_id"].nunique())
    return {"snapshots": snaps, "listings": listings, "rows": rows, "details": details}
