"""Prototype dashboard using job titles only: python -m src.sample_dashboard

A preview of the FR8 layout before tagging exists. Category and seniority come from regex
rules on the title alone (no team, no description, no LLM), so treat the numbers as rough.
Posting month = Greenhouse `published_at`, falling back to the first archive sighting.

Writes site/index.html and site/explorer.html with data embedded (opens from file://, deployable to GitHub Pages).
Only public, structured fields are embedded: month, category and seniority per job.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pandas as pd

from .common import RAW, ROOT, log, settings, setup_logging

ROWS_CACHE = RAW / "derived" / "sample_rows.parquet"
SALARY_CACHE = RAW / "derived" / "sample_salary.parquet"   # job_id, salary_min/max, currency only
OUT = ROOT / "site" / "index.html"
EXPLORER = ROOT / "site" / "explorer.html"
HOSTS = {"job_boards": "job-boards.greenhouse.io", "boards": "boards.greenhouse.io", "api": "job-boards.greenhouse.io"}

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


def _parse_to_cache() -> None:
    from .snapshots import load_captures, parse_all  # parse in memory; descriptions are not kept
    _, rows, details = parse_all(load_captures())
    ROWS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(ROWS_CACHE, index=False)
    sal = (details.dropna(subset=["salary_min"]).sort_values("observed_at").groupby("job_id")
                  .agg(salary_min=("salary_min", "last"), salary_max=("salary_max", "last"),
                       currency=("currency", "last")).reset_index())
    sal.to_parquet(SALARY_CACHE, index=False)


def load_rows() -> pd.DataFrame:
    if not ROWS_CACHE.exists():
        _parse_to_cache()
    return pd.read_parquet(ROWS_CACHE)


def load_salary() -> pd.DataFrame:
    if not SALARY_CACHE.exists():
        _parse_to_cache()
    return pd.read_parquet(SALARY_CACHE)


def build_jobs(rows: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    rows = rows.copy()
    rows["pub"] = pd.to_datetime(rows["published_at"], utc=True, format="mixed", errors="coerce")
    rows["observed_at"] = pd.to_datetime(rows["observed_at"], utc=True)
    rows = rows[rows["title"].notna()].sort_values("observed_at")
    jobs = rows.groupby("job_id").agg(title=("title", "last"), pub=("pub", "min"),
                                      first_seen=("observed_at", "min"), first_source=("source", "first"),
                                      team=("team", lambda s: s.dropna().iloc[-1] if s.notna().any() else None),
                                      location=("location", lambda s: s.dropna().iloc[-1] if s.notna().any() else None),
                                      last_seen=("observed_at", "max")).reset_index()
    jobs["date_source"] = jobs["pub"].notna().map({True: "published_at", False: "first_seen"})
    jobs["posted"] = jobs["pub"].fillna(jobs["first_seen"])
    jobs["month"] = jobs["posted"].dt.strftime("%Y-%m")

    # Months from backfill start to the last *complete* month of archive coverage.
    start = pd.Period(str(settings()["backfill_from"])[:7], "M")
    end = pd.Period(rows["observed_at"].max().strftime("%Y-%m"), "M") - 1
    months = [str(p) for p in pd.period_range(start, end, freq="M")]
    jobs = jobs[jobs["month"].isin(months)].copy()

    jobs = jobs.merge(load_salary(), on="job_id", how="left")
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
             f"https://web.archive.org/web/{fs.strftime('%Y%m%d%H%M%S')}/https://{HOSTS.get(src, HOSTS['job_boards'])}/anthropic/jobs/{jid}",
             num(lo), num(hi), cur if pd.notna(cur) else None]
            for jid, t, m, c, ckw, s, skw, team, loc, ls, fs, src, lo, hi, cur in zip(
                j["job_id"], j["title"], j["month"], j["category"], j["category_kw"], j["seniority"],
                j["seniority_kw"], j["team"], j["location"], j["last_seen"], j["first_seen"], j["first_source"],
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
    jobs, months = build_jobs(load_rows())
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
