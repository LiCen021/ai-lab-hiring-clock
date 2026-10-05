"""Prototype dashboard using job titles only: python -m src.sample_dashboard

A preview of the FR8 layout before tagging exists. Category and seniority come from regex
rules on the title alone (no team, no description, no LLM), so treat the numbers as rough.
Posting month = Greenhouse `published_at`, falling back to the first archive sighting.

Reads only data/processed/postings.parquet (public), so it runs in CI without raw data.
Writes site/index.html and site/explorer.html with data embedded (opens from file://, deployable to GitHub Pages).
Only public, structured fields are embedded: month, category and seniority per job.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pandas as pd

from .common import PROCESSED, ROOT, log, settings, setup_logging

POSTINGS = PROCESSED / "postings.parquet"   # public, derived data only; no raw input needed
OUT = ROOT / "site" / "index.html"
EXPLORER = ROOT / "site" / "explorer.html"

# Order = stack order = colour slot order (validated adjacent pairs). Other is neutral grey.
CATEGORIES = ["Software engineering", "Applied / ML engineering", "Research", "Sales & GTM",
              "Policy & legal", "Ops, finance & people", "Design & content", "Other"]

# First match wins, so the more specific patterns come first.
CATEGORY_RULES: list[tuple[str, str]] = [
    ("Design & content", r"\bUX\b|user experience|user research"),
    ("Applied / ML engineering", r"applied ai engineer|forward deployed"),
    ("Ops, finance & people", r"research operations|\bIT\b|technical program manager|\bTPM\b|recruit|"
                              r"accounting|accounts (payable|receivable)|payroll|compensation|\btax\b|"
                              r"transfer pricing|treasury|\bFX\b|capital markets|corporate development|"
                              r"order management|order-to-cash|billing|transaction|real estate|"
                              r"warehouse|logistics|hardware lab|\bHRIS\b|salesforce admin|vendor|"
                              r"planning|strategist|international expansion"),
    ("Sales & GTM", r"applied ai architect|solutions? (architect|executive)|architecture|sales engineer|"
                    r"account (executive|manager|director)|\bAE\b|customer success|technical success|"
                    r"business development|partner|\bGTM\b|go-to-market|marketing|\bsales\b|"
                    r"revenue(?! accounting)|deployment lead|startup|customer|demand gen|\bSDR\b|\bBDR\b|"
                    r"channel|alliances|industry|life sciences|healthcare|enablement|\bSEO\b|paid social"),
    ("Research", r"research (scientist|engineer|lead|manager|team)|\bresearcher\b|fellow|"
                 r"alignment|interpretability|frontier red team"),
    ("Applied / ML engineering", r"applied ai|forward deployed|machine learning|\bML\b|"
                                 r"data scien|\bAI engineer\b|model (behavior|quality)"),
    ("Software engineering", r"software|engineer|developer|\bSRE\b|site reliability|infrastructure|"
                             r"engineering manager|security architect|\bTech Lead\b"),
    ("Policy & legal", r"counsel|legal|paralegal|polic(y|ies)|\bcontracts\b|compliance|regulat|"
                       r"external affairs|government|public affairs|local affairs|privacy|trade|economist|"
                       r"societal|national security|global impact|public health"),
    ("Design & content", r"design|writer|writing|content|communications?|brand|editor|video|"
                         r"creative|\bcopy\b|social media|education|media relations|community|"
                         r"curriculum|certification"),
    ("Ops, finance & people", r"financ|accounting|accountant|tax|treasury|payroll|procure|"
                              r"recruit|sourc(er|ing)|talent|people|\bHR\b|benefits|workplace|"
                              r"operations|\bops\b|program manager|product manager|analyst|"
                              r"business partner|administrative|assistant|executive assistant|"
                              r"strategy|investigat|enforcement|safeguards|trust|support|"
                              r"facilities|security|chief of staff|coordinator|specialist|"
                              r"\bdata\b|product"),
]

SENIORITY = ["junior", "mid", "senior", "staff+", "manager"]
SENIORITY_RULES: list[tuple[str, str]] = [
    ("manager", r"\bhead of\b|(?<!art )(?<!creative )\bdirector\b|\bvp\b|vice president|\bchief\b(?! of staff)|"
                r"^(senior |sr\.? )?manager\b|manager of|"
                r"(engineering|research|team|people|sales|recruiting) manager"),
    ("junior", r"\bjunior\b|new grad|\bassociate\b|\bintern(ship)?\b|entry|early career|apprentice|"
               r"residency|\bfellows?\b"),
    ("staff+", r"\bstaff\b|principal|distinguished|\bfellow\b"),
    ("senior", r"\bsenior\b|\bsr\.?\b|\blead\b"),
]


def match_rule(title: str, rules: list[tuple[str, str]], default: str) -> tuple[str, str]:
    """Return (label, matched keyword); the keyword is shown in the explorer as the reason."""
    for label, pattern in rules:
        if m := re.search(pattern, title, re.I):
            return label, m.group(0)
    return default, ""


def first_match(title: str, rules: list[tuple[str, str]], default: str) -> str:
    return match_rule(title, rules, default)[0]


def load_postings() -> pd.DataFrame:
    if not POSTINGS.exists():
        raise SystemExit(f"{POSTINGS.relative_to(ROOT)} missing: run `python -m src.run_all --skip-fetch` first")
    return pd.read_parquet(POSTINGS)


def build_jobs(post: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    jobs = post[post["title"].notna()].copy()
    jobs["first_seen"] = pd.to_datetime(jobs["first_seen"])
    jobs["last_seen"] = pd.to_datetime(jobs["last_seen"])
    pub = pd.to_datetime(jobs["first_published"])
    jobs["date_source"] = pub.notna().map({True: "published_at", False: "first_seen"})
    jobs["posted"] = pub.fillna(jobs["first_seen"])
    jobs["month"] = jobs["posted"].dt.strftime("%Y-%m")

    # Months from backfill start to the last *complete* month of archive coverage.
    start = pd.Period(str(settings()["backfill_from"])[:7], "M")
    end = pd.Period(jobs["last_seen"].max().strftime("%Y-%m"), "M") - 1
    months = [str(p) for p in pd.period_range(start, end, freq="M")]
    jobs = jobs[jobs["month"].isin(months)].copy()

    usd = jobs["currency"] == "USD"
    jobs["usd_mid"] = ((jobs["salary_min"] + jobs["salary_max"]) / 2).where(usd).round(-3)
    jobs[["category", "category_kw"]] = [match_rule(t, CATEGORY_RULES, "Other") for t in jobs["title"]]
    jobs[["seniority", "seniority_kw"]] = [match_rule(t, SENIORITY_RULES, "mid") for t in jobs["title"]]
    return jobs, months


def write_explorer(jobs: pd.DataFrame, months: list[str]) -> None:
    """Posting-level page: public, structured fields only (no descriptions)."""
    j = jobs.sort_values(["posted", "title"], ascending=[False, True])
    cols = ["id", "title", "month", "category", "category_kw", "seniority", "seniority_kw",
            "team", "location", "last_seen", "archive", "salary_min", "salary_max", "currency"]
    num = lambda v: None if pd.isna(v) else int(v)
    rows = [[jid, t, m, c, ckw, s, skw, team if pd.notna(team) else "", loc if pd.notna(loc) else "", ls.strftime("%Y-%m-%d"),
             au if pd.notna(au) else None,
             num(lo), num(hi), cur if pd.notna(cur) else None]
            for jid, t, m, c, ckw, s, skw, team, loc, ls, au, lo, hi, cur in zip(
                j["job_id"], j["title"], j["month"], j["category"], j["category_kw"], j["seniority"],
                j["seniority_kw"], j["team"], j["location"], j["last_seen"], j["archive_url"],
                j["salary_min"], j["salary_max"], j["currency"])]
    data = {"columns": cols, "rows": rows, "months": months, "categories": CATEGORIES, "seniority": SENIORITY,
            # Each regex split into its alternatives, so no published field is a long string.
            "rules": [[label, pattern.split("|")] for label, pattern in CATEGORY_RULES],
            "generated": pd.Timestamp.now().strftime("%Y-%m-%d")}
    EXPLORER.write_text((ROOT / "src" / "templates" / "job_explorer.html").read_text()
                        .replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))))
    print(EXPLORER)


def main() -> None:
    setup_logging()
    jobs, months = build_jobs(load_postings())
    cat_ix = {c: i for i, c in enumerate(CATEGORIES)}
    sen_ix = {s: i for i, s in enumerate(SENIORITY)}
    m_ix = {m: i for i, m in enumerate(months)}
    data = {
        "months": months,
        "categories": CATEGORIES,
        "seniority": SENIORITY,
        # [month index, category index, seniority index, USD salary midpoint or null] per job
        "jobs": [[m_ix[m], cat_ix[c], sen_ix[s], None if pd.isna(p) else int(p)]
                 for m, c, s, p in zip(jobs["month"], jobs["category"], jobs["seniority"], jobs["usd_mid"])],
        "salary_coverage": round(float(jobs["salary_min"].notna().mean()), 3),
        "low_coverage": [[str(a)[:7], str(b)[:7]] for a, b in settings()["low_coverage_periods"]],
        "benchmark": {"hirebase_2025": settings()["benchmark"]["hirebase_2025"]},
        "n_first_seen_fallback": int((jobs["date_source"] == "first_seen").sum()),
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text((ROOT / "src" / "templates" / "sample_dashboard.html").read_text()
                   .replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))))
    write_explorer(jobs, months)

    log.info("%d jobs, %s → %s", len(jobs), months[0], months[-1])
    log.info("category mix: %s", dict(Counter(jobs["category"]).most_common()))
    log.info("seniority mix: %s", dict(Counter(jobs["seniority"]).most_common()))
    other = jobs[jobs["category"] == "Other"]["title"]
    log.info("Other: %d (%.1f%%), e.g. %s", len(other), 100 * len(other) / len(jobs), other.head(15).tolist())
    print(OUT)


if __name__ == "__main__":
    main()
