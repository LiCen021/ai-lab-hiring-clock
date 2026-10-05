# AI Lab Hiring Clock

**What does an AI lab hire for as it scales, and is the engineering role changing?**

This project tracks how Anthropic's hiring mix changes over time, using archived snapshots of its public Greenhouse job board. The headline visual is a "doom clock" for software-engineering roles: minutes to midnight move with software engineering's share of new job postings.

> The clock is here to catch your eye, not to prove anything. One company's hiring mix mostly tracks its growth stage, funding and strategy. If the engineering share drops, that alone doesn't mean robots took the keyboards.

**Dashboard:** [licen021.github.io/ai-lab-hiring-clock](https://licen021.github.io/ai-lab-hiring-clock/) (GitHub Pages; the clock, then tabs for hiring mix, engineering, salary and a job explorer)

## Status: prototype

The dashboard currently classifies jobs by **keyword rules on the job title only**. Description-based tagging (LLM, fixed vocabulary), title + team categorisation, manual overrides and tagger validation are still to be built (see `ARCHITECTURE.md` §9). Treat all shares as rough.

Preliminary, title-only readings (Jan 2025 – Sep 2026, 2,349 postings):

- Software engineering's share of new postings is roughly flat: 20% in the latest 3 months vs 17% a year earlier and 19% in early 2025, so the clock reads 11:28.
- New postings roughly doubled (about 148 a month vs 76 a year earlier), mostly in Sales & GTM and Ops, finance & people.
- Junior engineering postings are almost absent, and the engineering mix has shifted toward senior and staff roles.

## How the clock works

`minutes_to_midnight = 30 × (current SWE share ÷ baseline SWE share)`, clamped to 0–60.

- *Current* = software engineering's share of new postings over the trailing 3 months.
- *Baseline* = the same share in the first full quarter of data (Jan–Mar 2025), fixed so it doesn't drift.
- The clock shows `12:00 − minutes`; fewer minutes means engineering is a smaller part of hiring.

## Data

- **Source:** Wayback Machine snapshots of `job-boards.greenhouse.io/anthropic`, `boards.greenhouse.io/anthropic` and the Greenhouse boards API, from 2025-01-01. Archived individual job pages supply most of the coverage, because the current board only shows 50 jobs per page.
- **Posting date:** Greenhouse `published_at` (99% of jobs), else the first archive sighting.
- **Benchmark:** 1,009 unique postings first published in 2025, vs 893 in Hirebase's archive (Hirebase counts multi-location roles once per location; `published_at` can reset when a role is reposted).
- **Salary:** posted base ranges (sales roles show on-target earnings; equity not included). This shows where posted pay points, not actual spend.

## Privacy

Description text is used only transiently and privately. The public repo and site contain only structured, derived data (title, team, location, dates, salary range, category, seniority). Raw snapshots stay in a git-ignored `data/raw/` and a separate private archive. The site is built in CI from `data/processed/` alone, so it never sees raw data. A privacy guard (`tests/test_privacy.py`) runs on `data/processed/` and the built site, fails if any published field is longer than 300 characters or contains an email address or phone number, and blocks the Pages deploy.

## Running it

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.fetch_wayback          # download archive snapshots (cached; ~1 req/s)
.venv/bin/python -m src.run_all --skip-fetch   # parse + dedupe → data/processed/ (commit this)
.venv/bin/python -m src.sample_dashboard       # local preview: data/processed/ → site/*.html
.venv/bin/python -m pytest                     # tests, including the privacy guard
```

Only `data/processed/` is committed. On push, `.github/workflows/pages.yml` builds `site/*.html` from it, runs the privacy guard and deploys to GitHub Pages. To rebuild the site without the raw archive, clone the repo and run `sample_dashboard` alone.

## Limitations

Single company; snapshot granularity of about ±1 week; coverage gaps (Dec 2025–Jan 2026 has fewer board snapshots); title-only classification error; and hiring mix ≠ AI displacement.

---

Data derived from Anthropic's public Greenhouse job board and Internet Archive snapshots; descriptions are not redistributed.
