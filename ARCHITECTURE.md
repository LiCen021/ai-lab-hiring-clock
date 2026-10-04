# AI Lab Hiring Clock — Architecture

> How the system is built. For what it must do and why, see `REQUIREMENTS.md`.

## 1. System overview

```
                    ┌──────────────────────────────┐
  Wayback Machine ─▶│  Phase 1: backfill (local)   │
  (3 board URLs)    └──────────────┬───────────────┘
                                   │ raw snapshots
  Greenhouse API ──▶ Phase 2: weekly GitHub Action
                                   │
                                   ▼
        ┌─────────────────────────────────────────────────┐
        │ parse → dedupe → scrub PII → tag → categorise    │
        │        → metrics → build site                    │
        └──────┬──────────────────────────────┬───────────┘
               │ raw text, raw JSON,          │ structured, derived data only
               │ raw LLM responses            │
               ▼                              ▼
   ┌──────────────────────────┐   ┌───────────────────────────────┐
   │ PRIVATE repo             │   │ PUBLIC repo                   │
   │ ai-lab-hiring-raw        │   │ ai-lab-hiring-clock           │
   │ append-only archive      │   │ code, processed data, site    │
   └──────────────────────────┘   └───────────────┬───────────────┘
                                                  │
                                                  ▼
                                       GitHub Pages dashboard
```

**Join key across repos:** `job_id` (+ `description_sha256` for the exact text version, + `tagged_from` for the raw file path).

## 2. Repositories

### 2.1 Public: `ai-lab-hiring-clock`

```
ai-lab-hiring-clock/
├── README.md                  # findings first, charts, method, limitations
├── REQUIREMENTS.md
├── ARCHITECTURE.md
├── requirements.txt
├── config/
│   ├── tags.yaml              # fixed tag vocabulary (enums) + regex patterns
│   ├── categories.yaml        # category/seniority rules, category→SOC map
│   └── settings.yaml          # sources, date range, model ID, prompt_version
├── src/
│   ├── run_all.py             # orchestrator: python -m src.run_all [--phase backfill|live]
│   ├── fetch_wayback.py       # CDX listing + cached, rate-limited download
│   ├── fetch_live.py          # Greenhouse API snapshot
│   ├── parse/
│   │   ├── greenhouse_api.py  # JSON parser (shared by backfill and live)
│   │   └── greenhouse_html.py # one function per markup era, if needed
│   ├── snapshots.py           # snapshot registry, suspect detection
│   ├── dedupe.py              # sightings → postings lifecycle
│   ├── text/
│   │   ├── clean.py           # HTML→text, boilerplate stripping
│   │   └── pii.py             # regex + Presidio scrub, audit counts
│   ├── tag/
│   │   ├── llm.py             # OpenRouter client, schema validation, cache
│   │   ├── regex_fields.py    # salary, years of experience
│   │   └── schema.py          # Pydantic models generated from tags.yaml
│   ├── categorise.py          # rules + overrides + SOC mapping
│   ├── metrics.py             # monthly series, headline numbers, clock time
│   ├── build_site.py          # writes site/data/dashboard.json + static PNG/SVG
│   └── raw_store.py           # read/write the private raw repo
├── site/
│   ├── index.html             # static dashboard (Plotly.js), reads data/dashboard.json
│   └── data/dashboard.json    # generated
├── data/
│   ├── raw/                   # GITIGNORED: local working copy of raw files
│   ├── processed/             # public, structured outputs (see §4)
│   ├── overrides.csv
│   └── labels.csv             # 50 hand-labelled postings for validation
├── notebooks/analysis.ipynb
├── tests/
│   ├── test_privacy.py        # CI privacy guard
│   ├── test_parsers.py        # fixtures from real snapshots (stored as minimal excerpts)
│   └── test_metrics.py
└── .github/workflows/
    ├── weekly.yml             # Phase 2 collection + rebuild + deploy
    └── ci.yml                 # tests + privacy guard on every push/PR
```

### 2.2 Private: `ai-lab-hiring-raw`

Append-only. Files are never edited in place.

```
ai-lab-hiring-raw/
├── wayback/<source>/<timestamp>.(html|json).gz   # one-off backfill
├── live/<YYYY-MM-DD>.json.gz                     # weekly API, full descriptions
└── llm_tags/<YYYY-MM-DD>.jsonl                   # raw LLM requests/responses
```

- Query locally with DuckDB directly over the files, e.g. `SELECT … FROM 'live/*.json.gz'`. No database file is committed (binary DB files bloat git history).
- Expected size is well under 100 MB per year gzipped.

## 3. Pipeline stages

| # | Stage | Module | Input | Output |
|---|---|---|---|---|
| 1 | Discover | `fetch_wayback` | 3 board URLs | snapshot list (CDX) |
| 2 | Fetch | `fetch_wayback` / `fetch_live` | snapshot list / API | raw files → `data/raw/` + private repo |
| 3 | Parse | `parse/*`, `snapshots` | raw files | `sightings.parquet`, `snapshots.csv` |
| 4 | Dedupe | `dedupe` | sightings + snapshots | posting lifecycle per `job_id` |
| 5 | Clean + scrub | `text/*` | descriptions (in memory) | scrubbed text (in memory), `pii_audit.csv` |
| 6 | Tag | `tag/*` | scrubbed text | `tags.parquet` (cached), raw responses → private repo |
| 7 | Categorise | `categorise` | postings + tags + overrides | category, seniority, SOC |
| 8 | Metrics | `metrics` | postings | monthly series, headline numbers, clock time |
| 9 | Build site | `build_site` | metrics | `site/data/dashboard.json`, README images |

Every stage is idempotent and reads cached outputs from previous stages, so re-runs skip completed work.

### 3.1 Fetch (Wayback)

- **Discovery:** `https://web.archive.org/cdx/search/cdx?url=<URL>&output=json&fl=timestamp,original,statuscode,mimetype,digest&filter=statuscode:200&from=2025`. Use `matchType=prefix` for the API URL to include query-string variants.
- **Raw content:** `https://web.archive.org/web/<timestamp>id_/<original>` (the `id_` suffix returns the original bytes, without the Wayback toolbar or rewritten links).
- Single-threaded, ~1 request/second, exponential backoff on 429/5xx (5 retries), descriptive User-Agent.
- Skip downloads already in the cache. Snapshots with an identical `digest` to their predecessor are still recorded as sightings, but parsed only once.

### 3.2 Parse

- **API JSON:** extract `id`, `title`, `location.name`, `departments`, `offices`, `absolute_url`, `updated_at`, `first_published` (if present), `content` (if present).
- **HTML:** first inspect sample snapshots from each period. Prefer an embedded JSON state blob if one exists; otherwise parse job links (`/anthropic/jobs/<id>`), section headings (department) and location. Check for pagination; if present, include archived paginated URLs or record the limitation.
- **Suspect detection:** a snapshot is `suspect` if its job count is <50% of the rolling median of the 5 neighbouring valid snapshots.

### 3.3 Dedupe and lifecycle

- `first_seen` / `last_seen` come from sightings across all sources, ordered by time.
- `still_open`: present in the most recent *valid* snapshot.
- `reappearances`: the count of gaps where the job is absent from ≥1 valid snapshot and later returns.
- Title, team and location take values from the latest sighting; `attributes_changed` flags any change.
- Synthetic IDs for ID-less HTML rows: `syn_` + first 10 hex characters of `sha256(title_normalised | location | first_seen)`.

### 3.4 Clean, scrub, tag

```
content_html → html_to_text → strip_boilerplate → pii_scrub → [regex fields] + [LLM tags]
```

- **Boilerplate:** split into paragraphs, hash each; any paragraph hash appearing in >50% of postings is boilerplate. Compute this once over the corpus, store the hash list in `data/processed/boilerplate_hashes.txt` (hashes only), and reuse it in Phase 2.
- **PII:** regex for emails, phone numbers and LinkedIn/GitHub/personal URLs, plus Microsoft Presidio (spaCy NER) for PERSON entities, replaced with placeholders such as `[PERSON]`. Write `pii_audit.csv` (job_id, pii_type, count).
- **LLM tagging:**
  - OpenRouter, OpenAI-compatible chat completions, `response_format` = JSON schema generated from `config/tags.yaml` (all values are enums or bools).
  - Pinned model ID and `prompt_version` from `config/settings.yaml`; temperature 0.
  - Cache key: `(job_id, description_sha256)`. If the key exists, skip the call.
  - Validate with Pydantic. On failure, retry once, then set `tag_status = pending`.
  - Append the full request/response to `llm_tags/<date>.jsonl` in the private repo.
- **Regex fields:** `salary_min`, `salary_max`, `currency` from the salary-range pattern; `years_experience_min` from `(\d+)\+?\s*(?:years|yrs)` (smallest value found).

### 3.5 Metrics and clock

Computed as defined in REQUIREMENTS FR7. The clock baseline (first full quarter) is computed once and stored in `dashboard.json`, so it doesn't drift as new data arrives.

## 4. Data schemas (public, `data/processed/`)

### `snapshots.csv`
`observed_at, source, original_url, job_count, status` (`ok` / `suspect` / `parse_error` / `duplicate_digest`)

### `sightings.parquet`
`job_id, observed_at, source`. Long format, one row per job per snapshot; drives "open roles over time".

### `postings.csv` / `postings.parquet`

| column | type | notes |
|---|---|---|
| job_id | str | Greenhouse ID, or `syn_…` |
| title | str | as posted (latest) |
| title_normalised | str | lowercased, punctuation stripped |
| team | str | Greenhouse department |
| location | str | raw |
| location_group | str | SF, NYC, Seattle, London, Dublin, Remote, Other… |
| category | str | FR6 list |
| seniority | str | junior / mid / senior / staff+ / manager / unknown |
| soc_major_group | str | code + name |
| ai_skills | list[str] | from LLM, fixed vocab |
| tech_skills | list[str] | from LLM, fixed vocab |
| focus_area | str | from LLM |
| is_people_manager | bool | from LLM |
| degree / degree_required | str / bool | from LLM |
| work_arrangement | str | from LLM |
| salary_min / salary_max / currency | int / int / str | regex |
| years_experience_min | int | regex |
| first_seen / last_seen | date | |
| first_published | date | if available from the API |
| still_open | bool | |
| days_open_approx | int | ±1 week |
| n_snapshots / reappearances | int | |
| sources | str | which URLs it was seen in |
| description_sha256 | str | text version fingerprint (no text) |
| tagged_from | str | raw file path in the private repo |
| tag_model / prompt_version / tagged_at / tag_status | str | provenance |
| attributes_changed | bool | |

### Other files
- `coverage.csv`: month × source → snapshot count; gaps > 14 days listed
- `pii_audit.csv`: job_id, pii_type, count
- `boilerplate_hashes.txt`

### `site/data/dashboard.json`
Pre-aggregated series for every panel (monthly category shares, seniority mix, headline numbers now vs a year ago, clock time and baseline, radar quarters, coverage footnotes, low-coverage shading ranges) plus a slimmed posting-level array (job_id, first_seen, category, seniority, ai_skills) to drive the client-side date filter.

## 5. Dashboard (GitHub Pages)

- A single static `site/index.html` with Plotly.js from a CDN. Vanilla JS; no build step.
- Fetches `data/dashboard.json` on load. The date-range filter recomputes shares in the browser from the posting-level array (a few thousand rows, which is instant).
- The clock is a custom SVG dial; other panels are Plotly charts.
- Deployed by the `actions/deploy-pages` workflow from `site/`.
- `build_site.py` also renders PNG/SVG copies (Plotly + kaleido) into `docs/img/` for the README.

**Why not Streamlit or Heroku:** Heroku's $5 Eco dynos sleep after 30 minutes idle, and Streamlit Community Cloud hibernates idle apps. Either would show a cold start or a "wake up" screen to LinkedIn visitors. The data changes weekly, so a static page is faster, free and maintenance-free.

## 6. Workflows

### 6.1 `ci.yml` (every push and PR)
- Run `pytest`, including `test_privacy.py`, which fails if any file under `data/processed/` or `site/` has a field longer than 300 characters, an email match or a phone-number match.

### 6.2 `weekly.yml` (Phase 2)

```
on: schedule (weekly, e.g. Mondays) + workflow_dispatch
permissions: contents: write, pages: write, id-token: write

steps:
  1. checkout public repo
  2. checkout private repo (token: RAW_REPO_TOKEN) into ./raw-store
  3. fetch_live → write raw-store/live/<date>.json.gz
  4. run pipeline (python -m src.run_all --phase live)
       - new postings only are scrubbed and tagged (OPENROUTER_API_KEY)
       - raw LLM responses → raw-store/llm_tags/<date>.jsonl
  5. commit + push raw-store (private)
  6. run tests incl. privacy guard. On failure, stop before step 7.
  7. commit + push data/processed + site/data (public)
  8. deploy site/ to GitHub Pages
```

- **Secrets:** `OPENROUTER_API_KEY`, `RAW_REPO_TOKEN` (fine-grained PAT with Contents read/write on the private repo only).
- **Logging rule:** never log description text. Log counts and job IDs only.
- **No Actions artifacts** for raw data.
- **Partial failure:** if the LLM step fails, postings are written with `tag_status = pending` and the run continues; the next run retries pending items.

### 6.3 Phase 1 backfill (local)
`python -m src.run_all --phase backfill` on the user's machine, then push `data/raw/` contents to the private repo with `raw_store.py`, and commit processed outputs to the public repo.

## 7. Configuration (`config/settings.yaml`)

```yaml
company: anthropic
sources:
  - job-boards.greenhouse.io/anthropic
  - boards.greenhouse.io/anthropic
  - boards-api.greenhouse.io/v1/boards/anthropic/jobs
backfill_from: 2025-01-01
request_interval_seconds: 1.0
suspect_threshold: 0.5
boilerplate_threshold: 0.5
llm:
  provider: openrouter
  model: <pinned-model-id>      # exact ID, never an auto-updating alias
  temperature: 0
  prompt_version: v1
low_coverage_periods:
  - [2025-12-01, 2026-01-31]
benchmark:
  hirebase_2025: 893
```

## 8. Key dependencies

`requests`, `beautifulsoup4` (or `selectolax`), `pandas`, `pyarrow`, `pydantic`, `pyyaml`, `presidio-analyzer` + `presidio-anonymizer` + spaCy model, `openai` (pointed at OpenRouter's base URL), `plotly`, `kaleido`, `duckdb` (local analysis), `pytest`.

## 9. Build order for Claude Code

1. `fetch_wayback` + inspection of sample snapshots (report formats found, pagination, embedded JSON) → **pause for review**
2. Parsers + `snapshots` + `dedupe` → check the 2025 count vs 893 → **pause for review**
3. `text/clean` + `text/pii` + privacy test
4. `tag/*` on a 50-posting sample → compare with `labels.csv` → **pause for review**
5. Full tagging, `categorise`, `metrics`
6. `build_site` + `site/index.html` + README images
7. `raw_store` + push of backfill raw data to the private repo
8. `weekly.yml` + `ci.yml` (Phase 2)
