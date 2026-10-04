"""Helpers shared by the HTML and API parsers.

Every parser returns a ParsedPage. `rows` holds one dict per job listed on the page with the
keys in ROW_FIELDS; `detail` (job pages and API with content only) adds description HTML and
salary for those jobs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ROW_FIELDS = ["job_id", "title", "team", "location", "url", "published_at",
              "internal_job_id", "requisition_id"]
DETAIL_FIELDS = ["job_id", "description_html", "salary_min", "salary_max", "currency"]

JOB_ID = re.compile(r"/jobs/(\d+)|[?&]gh_jid=(\d+)")
MONEY = re.compile(r"([$£€])\s?([\d,]+(?:\.\d+)?)\s*([kK])?")
CURRENCY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR"}


@dataclass
class ParsedPage:
    format: str                      # remix_board | remix_job | legacy_board | legacy_job | api_json | unknown
    rows: list[dict] = field(default_factory=list)
    detail: list[dict] = field(default_factory=list)
    page: int | None = None
    total: int | None = None         # the board's own job count, when it reports one
    total_pages: int | None = None
    is_listing: bool = False         # True for board pages / API (a list of open jobs)


def job_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = JOB_ID.search(url)
    return (m.group(1) or m.group(2)) if m else None


def row(**kw) -> dict:
    out = {k: kw.get(k) for k in ROW_FIELDS}
    for k in ("job_id", "internal_job_id", "requisition_id"):
        if out[k] is not None:
            out[k] = str(out[k])
    for k in ("title", "team", "location"):
        if isinstance(out[k], str):
            out[k] = " ".join(out[k].split()) or None
    return out


MIN_PLAUSIBLE_SALARY = 1000          # placeholders such as "€1 — €1 EUR" occur


def _number(digits: str) -> float:
    # "210,000" / "210.000" (EU thousands separator) / "210000.00"
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", digits):
        return float(re.sub(r"[.,]", "", digits))
    return float(digits.replace(",", ""))


def money(s: str | None) -> int | None:
    if not s:
        return None
    m = MONEY.search(s)
    if not m:
        return None
    v = _number(m.group(2)) * (1000 if m.group(3) else 1)
    return int(v) if v >= MIN_PLAUSIBLE_SALARY else None


def salary_from_text(s: str | None) -> tuple[int | None, int | None, str | None]:
    """'£200,000 — £215,000 GBP' → (200000, 215000, 'GBP')."""
    if not s:
        return None, None, None
    found = MONEY.findall(s)
    if not found:
        return None, None, None
    vals = [v for f in found if (v := money("".join(f))) is not None]
    if not vals:
        return None, None, None
    cur = re.search(r"\b(USD|GBP|EUR|CAD|AUD|SGD|JPY|CHF|INR|KRW)\b", s)
    return min(vals), max(vals), cur.group(1) if cur else CURRENCY_SYMBOL.get(found[0][0])
