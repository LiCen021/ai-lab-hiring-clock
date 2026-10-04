"""Parser for boards-api.greenhouse.io/v1/boards/<board>/jobs (shared by backfill and live)."""
from __future__ import annotations

import html
import json

from .common import ParsedPage, row, salary_from_text


def parse_api(text: str) -> ParsedPage:
    d = json.loads(text)
    jobs = d.get("jobs") or []
    page = ParsedPage("api_json", total=(d.get("meta") or {}).get("total"), is_listing=True)
    for j in jobs:
        depts = j.get("departments") or []
        page.rows.append(row(
            job_id=j.get("id"), title=j.get("title"),
            team=depts[0].get("name") if depts else None,
            location=(j.get("location") or {}).get("name"),
            url=j.get("absolute_url"),
            published_at=j.get("first_published"),
            internal_job_id=j.get("internal_job_id"), requisition_id=j.get("requisition_id"),
        ))
        if j.get("content"):
            # ?content=true returns entity-escaped HTML; salary sits in the text (pay-range block).
            body = html.unescape(j["content"])
            lo, hi, cur = salary_from_text(_pay_block(body))
            page.detail.append({"job_id": str(j["id"]), "description_html": body,
                                "salary_min": lo, "salary_max": hi, "currency": cur})
    return page


def _pay_block(body: str) -> str | None:
    i = body.find("pay-range")
    return body[i:i + 600] if i >= 0 else None
