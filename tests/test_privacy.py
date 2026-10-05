"""Privacy guard (REQUIREMENTS §7.7): nothing published may carry description text or PII.

Fails if any file published has (data/processed/ files git would commit, and everything under
site/, which is built in CI and deployed whole whether or not git tracks it)
  * a data field longer than 300 characters (description text leaking through),
  * an email address, or
  * a phone-number match.

"Fields" are CSV cells, JSON / Parquet string values, lines of .txt files, and the string
values of the JSON blob embedded in generated HTML pages (`const D = {...};`).
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAX_FIELD = 300

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"(?<![\d/])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d/])"   # 415-555-0100, (415) 555 0100
    r"|\+\d{1,3}(?:[\s.-]\d{2,5}){2,4}(?!\d)"                                      # +44 20 7946 0958
)
EMBEDDED = re.compile(r"const D = (\{.*?\});\s*$", re.S | re.M)
TEXT_SUFFIXES = {".csv", ".json", ".txt", ".html", ".svg", ".md"}


def public_files() -> list[Path]:
    """data/processed files git would publish (tracked plus untracked-but-not-ignored), and all of site/."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "data/processed"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        paths = [ROOT / p for p in out.splitlines()]
    except (OSError, subprocess.CalledProcessError):
        paths = list((ROOT / "data/processed").rglob("*"))
    paths += (ROOT / "site").rglob("*")   # the whole folder is deployed, git-ignored build output included
    return sorted({p for p in paths if p.is_file()})


def walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(k)
            yield from walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk_strings(v)


def fields(path: Path):
    """Yield every data field in a public file."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                yield from row
    elif suffix == ".json":
        yield from walk_strings(json.loads(path.read_text(encoding="utf-8")))
    elif suffix == ".parquet":
        pd = pytest.importorskip("pandas")
        df = pd.read_parquet(path)
        for col in df.columns:
            yield str(col)
            if df[col].dtype == object or str(df[col].dtype).startswith("string"):
                yield from (v for v in df[col].dropna() if isinstance(v, str))
    elif suffix == ".txt":
        yield from path.read_text(encoding="utf-8").splitlines()
    elif suffix == ".html":
        for m in EMBEDDED.finditer(path.read_text(encoding="utf-8")):
            yield from walk_strings(json.loads(m.group(1)))


def violations(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    found = []
    for value in fields(path):
        if len(value) > MAX_FIELD:
            found.append(f"{rel}: field of {len(value)} chars (> {MAX_FIELD}): {value[:60]!r}…")
        if PHONE.search(value):
            found.append(f"{rel}: phone-number match in field {value[:80]!r}")
    if path.suffix.lower() in TEXT_SUFFIXES:   # emails anywhere in the file, not just data fields
        for m in EMAIL.finditer(path.read_text(encoding="utf-8", errors="replace")):
            found.append(f"{rel}: email address {m.group(0)!r}")
    return found


def test_public_outputs_have_no_long_fields_emails_or_phones():
    files = public_files()
    assert files, "no public files found under data/processed/ or site/"
    problems = [v for p in files for v in violations(p)]
    assert not problems, "privacy guard failed:\n" + "\n".join(problems[:50])


def test_generated_pages_embed_parseable_data():
    # If the embed marker changes, the guard above would silently check nothing in the HTML.
    for page in ["index.html", "explorer.html"]:
        path = ROOT / "site" / page
        if path.exists():
            assert EMBEDDED.search(path.read_text(encoding="utf-8")), f"no embedded data found in site/{page}"


@pytest.mark.parametrize("text", ["contact jane.doe@example.com", "call 415-555-0100", "(415) 555 0100",
                                  "+44 20 7946 0958"])
def test_detectors_catch_pii(text):
    assert EMAIL.search(text) or PHONE.search(text)


@pytest.mark.parametrize("text", ["2026-10-04", "4017331008", "https://web.archive.org/web/20250320221034/x",
                                  "$350,000 - $850,000", "San Francisco, CA | New York City, NY", "@media (x)"])
def test_detectors_ignore_normal_values(text):
    assert not EMAIL.search(text) and not PHONE.search(text)
