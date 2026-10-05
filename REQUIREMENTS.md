# AI Lab Hiring Clock — Requirements

> What the project must do and why. For how it is built, see `ARCHITECTURE.md`.

## 1. Overview

A data-analysis portfolio project that tracks how an AI lab's (Anthropic's) hiring mix changes over time, using its public Greenhouse job board. The headline visual is a "doom clock" for software-engineering roles, backed by trend charts that carry the real evidence.

**Framing:** "What does an AI lab hire for as it scales, and is the engineering role changing?" The clock is a visual hook, not a causal claim. One company's hiring mix mostly reflects growth stage, funding and strategy. The README, dashboard and any LinkedIn post must say so.

**Audience:** LinkedIn viewers and potential employers. Findings first, tooling second.

**Secondary goal:** demonstrate privacy-by-design data handling. Raw text is used only transiently and privately; the public repo and site contain only structured, derived data.

## 2. Phases

- **Phase 1 (ship first):** one-off historical backfill from Wayback Machine snapshots, cleaning, tagging, categorisation, metrics, a static dashboard on GitHub Pages, and a README with findings.
- **Phase 2:** a weekly GitHub Action collects live data from the Greenhouse API, tags new postings, archives raw data privately, and republishes the dashboard.

Phase 1 output formats must equal what Phase 2 produces, so Phase 2 only adds the scheduled job and no rework.

## 3. Data sources

### 3.1 Historical (Phase 1): Wayback Machine snapshots of three board addresses

1. `job-boards.greenhouse.io/anthropic` (current HTML board)
2. `boards.greenhouse.io/anthropic` (older HTML board)
3. `boards-api.greenhouse.io/v1/boards/anthropic/jobs` (JSON API, including query-string variants such as `?content=true`)

Combine all three to maximise coverage. Default date range: everything available from 2025-01-01 onward.

**Known coverage of address 1** (days per month with a snapshot):

| Period | Snapshot days/month | Assessment |
|---|---|---|
| Jan–Feb 2025 | 0 | Missing; try addresses 2 and 3 |
| Mar–Nov 2025 | 9–20 | Excellent |
| Dec 2025–Jan 2026 | 2 | Weak (~fortnightly); try to fill from 2 and 3 |
| Feb–Sep 2026 | 5–8 | ~Weekly |

### 3.2 Live (Phase 2)

`https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true`, fetched weekly.

### 3.3 Benchmark (validation only)

Hirebase's commercial archive reports **893** Anthropic postings first posted in 2025 and **1,193** in 2026 (as of 2026-10-04). This is a sanity check, not ground truth. Hirebase counts multi-location roles once per location, and its coverage before 2025 is thin.

## 4. Functional requirements

### FR1. Collection
- List and download all usable snapshots of the addresses in §3.1. Cache every raw snapshot so re-runs never re-download.
- Be polite to archive.org: about 1 request/second, with retries and backoff.
- The backfill must run on the user's own machine (some sandboxes block archive.org).
- Phase 2: fetch the live API weekly.

### FR2. Parsing
- Parse both HTML board pages and API JSON. Per job, extract: Greenhouse job ID, title, department/team, location, URL, and description when present.
- Before writing HTML parsers, inspect sample snapshots from different periods to check for client-side-rendered data or pagination that would cause under-counting.
- Flag snapshots whose job count is anomalously low (e.g. <50% of the rolling median) as `suspect`. Suspect snapshots must not be treated as evidence that a job closed.

### FR3. Deduplication and lifecycle
- Key on Greenhouse job ID. When an older HTML snapshot lacks an ID, generate a stable synthetic ID: `syn_` + a short hash of normalised title, location and first-seen date. Use the same synthetic ID in every repo.
- Per job, record `first_seen`, `last_seen`, `still_open`, `n_snapshots`, `days_open_approx` (accurate to about ±1 week), number of reappearances after gaps, and which sources it was seen in.

### FR4. Description processing and privacy
- Description text is processed transiently to derive tags, then discarded from public outputs.
- Steps: HTML→text, strip shared boilerplate (paragraphs appearing in more than 50% of postings, e.g. "About Anthropic", benefits, EEO), PII scrub, tag.
- PII scrub must remove emails, phone numbers, personal profile URLs and person names *before* any text leaves the machine or runner (including to the LLM). Log only counts of PII found per type, never the values.
- Store `description_sha256` publicly so the exact text version can be identified without publishing it.

### FR5. Tagging
- **Primary:** an LLM tagger via OpenRouter, returning structured JSON validated against a schema with a **fixed tag vocabulary** (enums). The model may not invent tags.
- **Deterministic fields via regex:** salary range, minimum years of experience.
- Each posting is tagged **once**, keyed by `job_id` + `description_sha256`. Never re-tag history automatically, because a model change could then masquerade as a hiring trend. Record `model`, `prompt_version` and `tagged_at` on every result.
- Pin an exact model ID and use temperature 0. Only use models that reliably support structured outputs. Enable OpenRouter's setting restricting routing to providers that don't log or train on prompts.
- On API failure or invalid output, mark `tag_status = pending` and retry on the next run. Never fail the whole run.
- **Validation:** hand-label 50 random postings in `data/labels.csv`; report per-tag precision and recall in the README.

**Tag vocabulary** (configurable in one file):
- `ai_skills`: agents/agentic, evals, MCP, Claude Code, RLHF, fine-tuning, prompting, LLM
- `tech_skills`: e.g. Python, Rust, TypeScript, Kubernetes, GPU/CUDA, distributed systems
- `degree`: PhD / Master's / Bachelor's / none stated, plus `degree_required` (bool)
- `work_arrangement`: remote / hybrid / in-office
- `is_people_manager`: bool
- `focus_area`: one value from a fixed list, e.g. pretraining, alignment, interpretability, product, infra, security, data, GTM, policy, ops

### FR6. Categorisation
- Rule-based categorisation (regex on title + team), with a manual override file (`data/overrides.csv`: job_id → category/seniority). The LLM `focus_area` may be used as a tiebreaker.
- Report the share of postings categorised as Other/unknown; target under 5%.

**Categories:** Research · Software engineering (product, infra, security, data eng) · Applied / ML engineering · Sales & GTM · Policy & legal · Ops, finance & people · Design & content · Other

**Seniority (from title):** junior/new grad/associate → junior; senior → senior; staff/principal/distinguished → staff+; manager/head/director/lead (people management) → manager; otherwise → mid.

**SOC major group:** map each category to the closest SOC major group (e.g. SWE → 15-0000 Computer & Mathematical) for comparison with labour-market and AI-exposure data.

### FR7. Metrics
Monthly buckets by `first_seen` (new postings). Headline numbers use a trailing 3-month window.

- **SWE share:** SWE new postings ÷ all new postings
- **Junior share of engineering:** junior ÷ (SWE + Applied ML) new postings
- **AI-skill mention rate:** share of engineering postings with any `ai_skills` tag
- **Category share over time:** each category's share of new postings per month
- **Category growth:** new-posting count per category, first 6 months vs last 6 months
- **Open duration:** median `days_open_approx` by category, excluding still-open roles
- **2025 unique postings:** reported alongside the Hirebase benchmark of 893, with a sentence explaining any gap

**Clock mapping:** `minutes_to_midnight = 30 × (current SWE share ÷ baseline SWE share)`, clamped to 0–60, where baseline = the first full quarter of data. Display as clock time (e.g. 11:38). Document this formula wherever the clock appears.

### FR8. Dashboard
A **static web page on GitHub Pages**. No server, no cold starts, free. It loads instantly from a LinkedIn link.

Panels, top to bottom:
1. **The clock:** analogue dial at the computed time, with the label "SWE share: X%" and a year-ago comparison.
2. **Key numbers card:** SWE share, junior share of engineering and AI-skill mention rate, each showing now vs a year ago with ▲/▼.
3. **100% stacked area chart:** share of new postings by category per month (the main trend view).
4. **Engineering seniority mix:** line chart of junior / mid / senior+ share over time.
5. **Radar, then vs now:** category shares for the first full quarter vs the latest quarter.
6. **Stretch, AI exposure vs hiring growth scatter:** x = AI exposure score per category (via SOC, if published data is available), y = posting growth, with the high-exposure, shrinking quadrant marked as the "doom zone". If no exposure data is available, skip it and document why.

Every chart shows a data-coverage footnote and shades Dec 2025–Jan 2026 as a lower-coverage period. A client-side date-range filter applies to all panels. The page also links to the methodology and limitations section of the README.

Static PNG/SVG exports of the clock and key charts are embedded in the README for viewers who don't click through.

### FR9. Raw data archive (private)
- All raw data (Wayback snapshots, live API JSON with full descriptions, raw LLM responses) is stored in a **separate private GitHub repo**, append-only.
- The public dataset keeps `job_id`, `description_sha256` and `tagged_from` (path of the raw file in the private repo), so any public row can be traced to its exact raw source.

## 5. Privacy and data-publication rules

| Data | Public repo / site | Private repo |
|---|---|---|
| Job ID, title, team, location, salary range, dates | ✅ | ✅ |
| Derived fields: category, seniority, SOC, tags | ✅ | ✅ |
| `description_sha256`, `tagged_from` | ✅ | ✅ |
| Full description text | ❌ | ✅ |
| Raw Wayback snapshots / raw API JSON | ❌ | ✅ |
| Raw LLM responses | ❌ | ✅ |
| PII values (names, emails, phones) | ❌ | ❌ (scrubbed before tagging; raw source may contain them) |
| PII counts per job | ✅ (audit file) | ✅ |

- Never use GitHub Actions artifacts for raw storage (they expire, and on public repos any logged-in user can download them).
- Never print description text in Action logs, which are public on public repos. Log counts and job IDs only.
- `data/raw/` is gitignored in the public repo.
- README attribution: "Data derived from Anthropic's public Greenhouse job board and Internet Archive snapshots; descriptions are not redistributed."

## 6. Non-functional requirements

- **Reproducible:** one command runs the whole Phase 1 pipeline end to end; re-runs use caches and don't re-fetch or re-tag.
- **Cheap:** $0 hosting; LLM tagging cost in cents per week.
- **Robust:** a failed source, snapshot or LLM call degrades gracefully (logged and retried later) and doesn't abort the run.
- **Secrets:** stored only as GitHub Actions secrets (`OPENROUTER_API_KEY`, `PUBLIC_REPO_TOKEN`), held by the private repo, which runs the weekly Action. The token is fine-grained, with write access to the public repo only, used to push `data/processed/`.
- **Language:** Python 3.11+.

## 7. Acceptance criteria

1. `python -m src.run_all` runs fetch → parse → dedupe → tag → categorise → metrics → site end to end, and a second run completes from cache with no re-downloads or re-tagging.
2. `coverage.csv` shows snapshots per month per source, and lists gaps longer than 14 days.
3. A spot check of 5 random snapshots shows parsed job counts matching a manual count of the archived page.
4. Under 5% of postings categorised as Other/unknown after overrides.
5. The 2025 unique-posting count is reported next to Hirebase's 893, with an explanation of any gap. A difference within about ±15% is expected; a larger shortfall must be investigated (pagination, parser) before publishing findings.
6. Tagger precision/recall on 50 hand-labelled postings is reported in the README.
7. The **privacy CI test** passes: no published file (committed under `data/processed/`, or anywhere in the built `site/`) contains a field longer than 300 characters, an email address or a phone-number match.
8. The dashboard is live on GitHub Pages with panels 1–5 (and 6 if data is available), the coverage footnotes, the shaded low-coverage period and a working date filter.
9. Phase 2: the weekly Action runs unattended, archives raw data to the private repo, commits updated processed data and republishes the site.
10. The README leads with 3–5 findings, then charts, method (including the clock formula and tagger validation), and a limitations section covering: single company, snapshot granularity of about ±1 week, coverage gaps, LLM tagging error, and that hiring mix ≠ AI displacement.
