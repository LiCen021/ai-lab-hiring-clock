"""Stage 4: collapse job sightings into one posting per Greenhouse job ID.

Key: the Greenhouse job-post ID, which is the same on the legacy board, the current board,
job pages and the API. Rows without an ID get a stable synthetic ID (`syn_` + hash).

Lifecycle uses only complete, non-suspect listings as evidence of absence:
  still_open     in the latest complete listing, or seen anywhere after it
  reappearances  absent from ≥1 complete listing between sightings, then back

Different job IDs with the same title + location are kept as separate postings but marked:
  duplicate_of   posted while an earlier copy was still live (parallel copy)
  repost_of      posted after the earlier copy disappeared (repost)
duplicate_audit.csv lists them for review. (Greenhouse internal_job_id is not used for this:
one requisition often carries several differently titled posts.)
"""
from __future__ import annotations

import hashlib
import re

import pandas as pd

from .common import PROCESSED, log
from .snapshots import DERIVED

# Wayback link to a job page: the host of the source it was first seen on (API jobs live on job-boards).
JOB_HOSTS = {"job_boards": "job-boards.greenhouse.io", "boards": "boards.greenhouse.io", "api": "job-boards.greenhouse.io"}

PUBLIC_COLS = ["job_id", "title", "title_normalised", "team", "location", "url", "first_seen", "last_seen",
               "first_published", "still_open", "days_open_approx", "n_snapshots", "reappearances",
               "sources", "seen_on", "archive_url", "internal_job_id", "requisition_id", "salary_min", "salary_max",
               "currency", "description_sha256", "tagged_from", "attributes_changed", "duplicate_of", "repost_of"]


def normalise_title(t: str | None) -> str | None:
    if not isinstance(t, str):
        return None
    return " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())


def assign_synthetic_ids(rows: pd.DataFrame) -> pd.DataFrame:
    """syn_ + first 10 hex of sha256(title_normalised | location | first_seen date)."""
    missing = rows["job_id"].isna()
    if not missing.any():
        return rows
    rows = rows.copy()
    m = rows[missing]
    keys = m["title"].map(normalise_title).fillna("") + "|" + m["location"].fillna("")
    first = m.groupby(keys)["observed_at"].transform("min").dt.strftime("%Y-%m-%d")
    rows.loc[missing, "job_id"] = [
        "syn_" + hashlib.sha256(f"{k}|{d}".encode()).hexdigest()[:10] for k, d in zip(keys, first)]
    log.info("assigned synthetic IDs to %d rows (%d jobs)", int(missing.sum()), rows.loc[missing, "job_id"].nunique())
    return rows


def latest_non_null(rows: pd.DataFrame, col: str) -> pd.Series:
    return rows.dropna(subset=[col]).sort_values("observed_at").groupby("job_id")[col].last()


def lifecycle(rows: pd.DataFrame, listings: pd.DataFrame) -> pd.DataFrame:
    good = listings[listings["complete"] & (listings["status"] == "ok")].sort_values("end")
    members = rows.dropna(subset=["listing_id"]).groupby("listing_id")["job_id"].agg(set)
    seq = [(r.end, members.get(r.listing_id, set())) for r in good.itertuples()]
    span = rows.groupby("job_id")["observed_at"].agg(["min", "max"])
    latest_at, latest_jobs = seq[-1] if seq else (pd.Timestamp.min.tz_localize("UTC"), set())
    out = {}
    for jid, (first, last) in span.iterrows():
        flags = [jid in jobs for t, jobs in seq if first <= t <= last]
        re_ = sum(1 for a, b in zip(flags, flags[1:]) if not a and b)
        out[jid] = {"still_open": jid in latest_jobs or last > latest_at, "reappearances": re_}
    log.info("lifecycle evidence: %d complete listings, latest %s", len(seq), latest_at)
    return pd.DataFrame.from_dict(out, orient="index")


def pick_detail(details: pd.DataFrame) -> pd.DataFrame:
    """Earliest captured description per job (the version as first posted); salary likewise."""
    d = details.copy()
    d["description_html"] = d["description_html"].where(d["description_html"].fillna("").str.strip() != "")
    desc = d.dropna(subset=["description_html"]).sort_values("observed_at").groupby("job_id").first()
    desc = pd.DataFrame({
        "description_sha256": desc["description_html"].map(lambda s: hashlib.sha256(s.strip().encode()).hexdigest()),
        "tagged_from": desc["raw_path"]})
    sal = (d.dropna(subset=["salary_min"]).sort_values("observed_at").groupby("job_id")
            [["salary_min", "salary_max", "currency"]].first())
    return desc.join(sal, how="outer")


def mark_same_role(post: pd.DataFrame) -> pd.DataFrame:
    """Within each (title_normalised, location) group, ordered by first_seen:
    a post whose lifetime overlaps an earlier one → duplicate_of (parallel copy);
    otherwise a later post → repost_of the most recent earlier one."""
    post = post.copy()
    post["duplicate_of"] = None
    post["repost_of"] = None
    key = post["title_normalised"].fillna("") + " | " + post["location"].fillna("").str.lower()
    for _, g in post[post["title_normalised"].notna()].groupby(key):
        if len(g) < 2:
            continue
        g = g.sort_values(["first_seen", "job_id"])
        for pos in range(1, len(g)):
            cur, earlier = g.iloc[pos], g.iloc[:pos]
            live = earlier[earlier["last_seen"] >= cur["first_seen"]]
            if len(live):
                post.loc[cur.name, "duplicate_of"] = live["job_id"].iloc[0]
            else:
                post.loc[cur.name, "repost_of"] = earlier["job_id"].iloc[-1]
    return post


def duplicate_audit(post: pd.DataFrame) -> pd.DataFrame:
    a = post[post["duplicate_of"].notna() | post["repost_of"].notna()]
    return pd.DataFrame({
        "job_id": a["job_id"], "relation": a["duplicate_of"].notna().map({True: "parallel", False: "repost"}),
        "of_job_id": a["duplicate_of"].fillna(a["repost_of"]), "title": a["title"], "location": a["location"],
        "first_seen": a["first_seen"], "last_seen": a["last_seen"]})


def run(rows: pd.DataFrame | None = None, listings: pd.DataFrame | None = None,
        details: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = rows if rows is not None else pd.read_parquet(DERIVED / "job_rows.parquet")
    listings = listings if listings is not None else pd.read_csv(PROCESSED / "listings.csv", parse_dates=["start", "end"])
    details = details if details is not None else pd.read_parquet(DERIVED / "details.parquet")
    rows = assign_synthetic_ids(rows)

    g = rows.groupby("job_id")
    post = pd.DataFrame({
        "first_seen": g["observed_at"].min(), "last_seen": g["observed_at"].max(),
        "n_snapshots": g["cap_id"].nunique(),
        "sources": g["source"].agg(lambda s: ";".join(sorted(set(s)))),
        "seen_on": g["kind"].agg(lambda s: ";".join(sorted(set(s)))),
    })
    first = rows.sort_values("observed_at").groupby("job_id")[["observed_at", "source"]].first()
    post["archive_url"] = [None if jid.startswith("syn_") else   # no job page to link to
                           f"https://web.archive.org/web/{t.strftime('%Y%m%d%H%M%S')}/https://"
                           f"{JOB_HOSTS.get(src, JOB_HOSTS['job_boards'])}/anthropic/jobs/{jid}"
                           for jid, t, src in zip(first.index, first["observed_at"], first["source"])]
    for col in ["title", "team", "location", "url", "internal_job_id", "requisition_id"]:
        post[col] = latest_non_null(rows, col)
    pub = pd.to_datetime(rows["published_at"], utc=True, errors="coerce", format="ISO8601")
    post["first_published"] = pub.groupby(rows["job_id"]).min().dt.date
    norm = rows.assign(t=rows["title"].map(normalise_title), l=rows["location"].str.lower(), m=rows["team"].str.lower())
    post["attributes_changed"] = norm.groupby("job_id")[["t", "l", "m"]].nunique().gt(1).any(axis=1)
    post = post.join(lifecycle(rows, listings)).join(pick_detail(details))
    post["title_normalised"] = post["title"].map(normalise_title)
    post["days_open_approx"] = (post["last_seen"] - post["first_seen"]).dt.days
    post["first_seen"] = post["first_seen"].dt.date
    post["last_seen"] = post["last_seen"].dt.date
    post = mark_same_role(post.rename_axis("job_id").reset_index())
    post = post[PUBLIC_COLS].sort_values(["first_seen", "job_id"])
    for c in ["salary_min", "salary_max", "n_snapshots", "reappearances", "days_open_approx"]:
        post[c] = post[c].astype("Int64")

    assert post["job_id"].is_unique, "duplicate job_id in postings"
    audit = duplicate_audit(post)
    post.to_csv(PROCESSED / "postings.csv", index=False)
    post.to_parquet(PROCESSED / "postings.parquet", index=False)
    audit.to_csv(PROCESSED / "duplicate_audit.csv", index=False)
    log.info("postings: %d unique job IDs from %d parsed rows; %d with description, %d with salary",
             len(post), len(rows), int(post["description_sha256"].notna().sum()), int(post["salary_min"].notna().sum()))
    log.info("same title+location: %s", audit["relation"].value_counts().to_dict())
    return post
