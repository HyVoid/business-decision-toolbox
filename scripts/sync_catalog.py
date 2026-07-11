#!/usr/bin/env python3
"""Regenerate the "## Available Decision Systems" section of each
catalog/<domain>.md page from catalog/tool-catalog.csv.

Field mapping (schema updated 2026-07; business_problem/pain_statement/
decision_question have been retired in favor of two columns that map
1:1 onto the published table column names):
    Decision System        <- repo_name           (cleaned for display)
    Helps You Decide       <- "Helps You Decide"   (sentence-cased, "?" appended)
    Primary Capability     <- "Primary Capability" (sentence-cased, "." appended)
    Repository              <- repository_url       (wrapped as a Markdown link)

The CSV is expected to already contain publication-ready text for the two
content columns (proper capitalization/punctuation) -- the sentence-casing
step below is a no-op safety net if that's already true, and only kicks in
if a row is ever pasted in lowercase/unpunctuated like the old convention.

Fan-out rule:
    A row is written into the table of EVERY domain listed in its
    `navigation_domains` column (semicolon-separated), not only its
    `primary_domain`. This matches the existing catalog: e.g. Paid-Media-Data-Hub
    has primary_domain=marketing but navigation_domains=marketing;data-architecture,
    and is intentionally listed in both marketing.md and data-architecture.md.
    If navigation_domains is blank for a row, primary_domain is used as the
    fallback so no row is silently dropped.

The section is located by its "## Available Decision Systems" heading
(matched case-insensitively, so a page still on the older
"## Available decision systems" capitalization is found and normalized
to the new heading text on its next sync rather than hard-failing) and
everything up to the next "## " heading (or end of file) is replaced --
so any content before or after that section is left untouched. The table's
column-header row is always regenerated from TABLE_HEADER below, so a page
does not need its column names fixed by hand first -- only the section
heading itself needs to already exist somewhere in the file.

Exit behavior:
    Exits non-zero with a clear message on: missing/malformed CSV columns,
    a domain named in the CSV with no matching catalog/<domain>.md file, or
    a missing section heading in a target file. Silent partial success is
    treated as a bug, not a feature -- see the "Automate repository catalog
    sync" / "Revert catalog automation" history in this repo for why.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = ROOT / "catalog"
CSV_PATH = CATALOG_DIR / "tool-catalog.csv"

REQUIRED_COLUMNS = [
    "repo_name",
    "primary_domain",
    "navigation_domains",
    "Helps You Decide",
    "Primary Capability",
    "repository_url",
]

SECTION_HEADING = "## Available Decision Systems"
TABLE_HEADER = (
    "| Decision System | Helps You Decide | Primary Capability | Repository |\n"
    "|---|---|---|---|"
)

# Tokens this short and fully uppercase are treated as acronyms and kept as-is
# (DTC, RTL, SKU, VAT, DIN, AIA, IIF, 3D, ...). Longer all-caps tokens (e.g. a
# repo_name typed in ALL CAPS) are treated as ordinary words and sentence-cased.
ACRONYM_MAX_LEN = 3


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"sync_catalog.py: {message}", file=sys.stderr)
    raise SystemExit(1)


def single_line(text: str) -> str:
    """Collapse embedded newlines/whitespace so a field can't break a table row."""
    return re.sub(r"\s+", " ", text).strip()


def escape_cell(text: str) -> str:
    """Escape pipes so a field can't break table column boundaries."""
    return single_line(text).replace("|", "\\|")


def _is_brand_mixed_case(tok: str) -> bool:
    """True for tokens like 'TikTok': an uppercase letter after position 0
    AND a lowercase letter somewhere too. Blind capitalize() would turn
    'TikTok' into 'Tiktok', silently destroying the brand's own casing."""
    rest = tok[1:]
    return any(c.isupper() for c in rest) and any(c.islower() for c in rest)


def humanize_repo_name(repo_name: str) -> str:
    tokens = [t for t in re.split(r"[-_]+", repo_name.strip()) if t]
    out = []
    for tok in tokens:
        if tok.isupper() and len(tok) <= ACRONYM_MAX_LEN:
            out.append(tok)  # short acronym: DTC, RTL, SKU, VAT, DIN, AIA, IIF, 3D...
        elif _is_brand_mixed_case(tok):
            out.append(tok)  # already-intentional internal caps: TikTok, etc.
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)


def as_question(text: str) -> str:
    text = single_line(text)
    if not text:
        return text
    text = text[0].upper() + text[1:]
    if not text.endswith("?"):
        text += "?"
    return text


def as_sentence(text: str) -> str:
    text = single_line(text)
    if not text:
        return text
    text = text[0].upper() + text[1:]
    if not text.endswith((".", "!", "?")):
        text += "."
    return text


def load_rows() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        fail(f"{CSV_PATH} not found")
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            fail(
                f"{CSV_PATH.name} is missing required column(s): {missing}. "
                f"Found columns: {fieldnames}"
            )
        rows = []
        for i, row in enumerate(reader, start=2):  # header is line 1
            repo = (row.get("repo_name") or "").strip()
            url = (row.get("repository_url") or "").strip()
            if not repo or not url:
                fail(f"{CSV_PATH.name} line {i}: repo_name and repository_url are required")
            rows.append(row)
    return rows


def group_by_domain(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    by_domain: dict[str, list[dict[str, str]]] = {}
    seen_per_domain: dict[str, set[str]] = {}
    for row in rows:
        repo = row["repo_name"].strip()
        nav_raw = (row.get("navigation_domains") or "").strip()
        domains = [d.strip() for d in nav_raw.split(";") if d.strip()]
        if not domains:
            domains = [row["primary_domain"].strip()]
        for domain in domains:
            seen = seen_per_domain.setdefault(domain, set())
            if repo in seen:
                continue  # same domain listed twice for one row -> keep first only
            seen.add(repo)
            by_domain.setdefault(domain, []).append(row)
    return by_domain


def build_table(rows_for_domain: list[dict[str, str]]) -> str:
    lines = [TABLE_HEADER]
    for row in rows_for_domain:
        name = escape_cell(humanize_repo_name(row["repo_name"]))
        question = escape_cell(as_question(row["Helps You Decide"]))
        capability = escape_cell(as_sentence(row["Primary Capability"]))
        url = row["repository_url"].strip()
        lines.append(f"| {name} | {question} | {capability} | [Repository]({url}) |")
    return "\n".join(lines)


def replace_section(md_text: str, md_name: str, new_table: str) -> str:
    # Case-insensitive: a page still reading "## Available decision systems"
    # (pre-migration capitalization) is found and normalized to the current
    # SECTION_HEADING text below, rather than hard-failing until someone
    # hand-edits every page's heading first.
    if SECTION_HEADING.lower() not in md_text.lower():
        fail(f"{md_name}: no '{SECTION_HEADING}' heading found")
    pattern = re.compile(
        rf"({re.escape(SECTION_HEADING)})\n.*?(?=\n## |\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    # Always substitute the canonical heading text (not m.group(1)), so an
    # old-cased heading is corrected to match SECTION_HEADING going forward.
    new_text, n = pattern.subn(lambda m: SECTION_HEADING + "\n\n" + new_table + "\n", md_text, count=1)
    if n == 0:
        fail(f"{md_name}: found '{SECTION_HEADING}' but could not match the section body")
    return new_text


def main() -> int:
    rows = load_rows()
    by_domain = group_by_domain(rows)

    unknown_domains = {
        domain: [r["repo_name"] for r in domain_rows]
        for domain, domain_rows in by_domain.items()
        if not (CATALOG_DIR / f"{domain}.md").exists()
    }
    if unknown_domains:
        details = "; ".join(f"{d} ({', '.join(repos)})" for d, repos in unknown_domains.items())
        fail(
            "CSV references domain(s) with no matching catalog/<domain>.md file: "
            f"{details}. Create the page first, or fix the domain name in the CSV."
        )

    # Phase 1: compute every file's new content in memory first, so a problem
    # in any one domain page (e.g. a renamed heading) aborts before anything
    # is written -- never a mix of updated and stale catalog pages on disk.
    planned: dict[Path, str] = {}
    for domain, domain_rows in sorted(by_domain.items()):
        md_path = CATALOG_DIR / f"{domain}.md"
        old_text = md_path.read_text(encoding="utf-8")
        new_table = build_table(domain_rows)
        planned[md_path] = replace_section(old_text, md_path.name, new_table)

    # Phase 2: everything validated -- now write.
    changed = []
    for md_path, new_text in planned.items():
        if new_text != md_path.read_text(encoding="utf-8"):
            md_path.write_text(new_text, encoding="utf-8")
            changed.append(md_path.name)

    all_md = {p.name for p in CATALOG_DIR.glob("*.md")}
    untouched = sorted(all_md - {f"{d}.md" for d in by_domain})
    if untouched:
        print(f"Note: no CSV rows reference these pages, left as-is: {untouched}")

    print(f"Synced {len(by_domain)} domain page(s); {len(changed)} file(s) changed: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
