from pathlib import Path

import pandas as pd

from src.dedupe import assign_synthetic_ids, lifecycle, mark_same_role
from src.parse.common import money, salary_from_text
from src.parse.greenhouse_api import parse_api
from src.parse.greenhouse_html import parse_html
from src.snapshots import assign_listings, build_listings, flag_page_suspects

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text()


def test_remix_board_page_rows_and_paging():
    p = parse_html(read("remix_board_p2.html"))
    assert p.format == "remix_board" and p.is_listing
    assert (p.page, p.total, p.total_pages) == (2, 52, 2)
    # the featured post repeats a listed job and must not be counted twice
    assert [r["job_id"] for r in p.rows] == ["4934439008", "5023394008"]
    assert p.rows[0]["team"] == "Public Policy"
    assert p.rows[1]["published_at"].startswith("2025-12-11")


def test_remix_job_page_detail():
    p = parse_html(read("remix_job.html"), "https://job-boards.greenhouse.io/anthropic/jobs/4017331008?gh_src=x")
    assert p.format == "remix_job" and not p.is_listing
    assert p.rows[0]["job_id"] == "4017331008"
    d = p.detail[0]
    assert (d["salary_min"], d["salary_max"], d["currency"]) == (350000, 850000, "USD")
    assert "About Anthropic" in d["description_html"] and "Role-specific" in d["description_html"]


def test_legacy_board_departments_and_no_duplicate_rows():
    p = parse_html(read("legacy_board.html"))
    assert p.format == "legacy_board" and p.total_pages == 1
    assert [r["job_id"] for r in p.rows] == ["4452492008", "4009165008"]
    assert [r["team"] for r in p.rows] == ["Communications & Marketing", "Research"]


def test_legacy_job_eu_salary_and_date():
    p = parse_html(read("legacy_job.html"))
    assert p.rows[0]["job_id"] == "4461539008"
    assert p.rows[0]["published_at"] == "2025-01-20"
    d = p.detail[0]
    assert (d["salary_min"], d["salary_max"], d["currency"]) == (210000, 240000, "EUR")


def test_api_rows_and_content():
    p = parse_api(read("api.json"))
    assert p.is_listing and p.total == 1
    assert p.rows[0]["team"] == "Public Policy"
    assert p.detail[0]["salary_min"] == 200000 and p.detail[0]["currency"] == "GBP"


def test_money_formats():
    assert money("$350,000") == 350000
    assert money("€210.000") == 210000
    assert money("$1") is None  # placeholder salaries are dropped
    assert salary_from_text("£200,000 — £215,000 GBP") == (200000, 215000, "GBP")


def _ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_paginated_pages_stitch_into_complete_listing():
    snaps = pd.DataFrame([
        {"cap_id": 0, "observed_at": _ts("2025-05-21 10:00"), "source": "job_boards", "format": "remix_board",
         "page": 1, "job_count": 2, "total": 3, "total_pages": 2},
        {"cap_id": 1, "observed_at": _ts("2025-05-22 09:00"), "source": "job_boards", "format": "remix_board",
         "page": 2, "job_count": 1, "total": 3, "total_pages": 2},
        {"cap_id": 2, "observed_at": _ts("2025-06-30 09:00"), "source": "job_boards", "format": "remix_board",
         "page": 1, "job_count": 2, "total": 3, "total_pages": 2},
    ])
    rows = pd.DataFrame({"cap_id": [0, 0, 1, 2, 2], "job_id": ["a", "b", "c", "a", "b"]})
    snaps = flag_page_suspects(assign_listings(snaps))
    listings = build_listings(snaps, rows)
    assert listings["complete"].tolist() == [True, False]
    assert listings["job_count"].tolist() == [3, 2]


def test_lifecycle_uses_only_complete_listings():
    listings = pd.DataFrame({"listing_id": ["L1", "L2", "L3"], "end": [_ts("2025-01-01"), _ts("2025-02-01"), _ts("2025-03-01")],
                             "complete": [True, True, True], "status": ["ok", "ok", "ok"]})
    rows = pd.DataFrame({
        "job_id": ["a", "a", "b", "c"],
        "listing_id": ["L1", "L3", "L1", None],          # a is missing from L2 then returns; c only on a job page
        "observed_at": [_ts("2025-01-01"), _ts("2025-03-01"), _ts("2025-01-01"), _ts("2025-04-01")],
    })
    lc = lifecycle(rows, listings)
    assert lc.loc["a", "reappearances"] == 1 and lc.loc["a", "still_open"]
    assert not lc.loc["b", "still_open"]
    assert lc.loc["c", "still_open"]  # seen after the latest complete listing


def test_synthetic_ids_are_stable():
    rows = pd.DataFrame({"job_id": [None, None], "title": ["Software Engineer!", "software engineer"],
                         "location": ["SF", "SF"], "observed_at": [_ts("2025-01-02"), _ts("2025-01-09")]})
    out = assign_synthetic_ids(rows)
    assert out["job_id"].nunique() == 1 and out["job_id"].iloc[0].startswith("syn_")
    assert assign_synthetic_ids(rows)["job_id"].iloc[0] == out["job_id"].iloc[0]


def test_same_role_marking():
    d = pd.Timestamp
    post = pd.DataFrame({
        "job_id": ["1", "2", "3"], "title_normalised": ["ae emea"] * 3, "location": ["Dublin"] * 3,
        "first_seen": [d("2025-01-01").date(), d("2025-01-10").date(), d("2025-06-01").date()],
        "last_seen": [d("2025-03-01").date(), d("2025-02-01").date(), d("2025-07-01").date()],
    })
    out = mark_same_role(post).set_index("job_id")
    assert out.loc["2", "duplicate_of"] == "1"          # overlaps job 1 → parallel copy
    assert out.loc["3", "repost_of"] == "2" and out.loc["3", "duplicate_of"] is None
