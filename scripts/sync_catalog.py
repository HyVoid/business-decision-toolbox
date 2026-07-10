#!/usr/bin/env python3
"""Regenerate the "## Available Decision Systems" section of each
catalog/<domain>.md page from catalog/tool-catalog.csv.

Field mapping:
    Decision System    <- repo_name
    Helps You Decide   <- helps_you_decide
    Primary Capability <- primary_capability
    Repository         <- repository_url

Rows are written into every domain listed in navigation_domains
(semicolon-separated). If blank, primary_domain is used.

Only the "## Available Decision Systems" section is replaced.
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
    "helps_you_decide",
    "primary_capability",
    "repository_url",
]

SECTION_HEADING = "## Available Decision Systems"

TABLE_HEADER = (
    "| Decision System | Helps You Decide | Primary Capability | Repository |\n"
    "|---|---|---|---|"
)

ACRONYM_MAX_LEN = 3


def fail(msg):
    print(f"sync_catalog.py: {msg}", file=sys.stderr)
    raise SystemExit(1)


def single_line(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def escape_cell(text):
    return single_line(text).replace("|", "\\|")


def _is_brand(tok):
    rest = tok[1:]
    return any(c.isupper() for c in rest) and any(c.islower() for c in rest)


def humanize_repo_name(name):
    out = []
    for tok in [t for t in re.split(r"[-_]+", name.strip()) if t]:
        if tok.isupper() and len(tok) <= ACRONYM_MAX_LEN:
            out.append(tok)
        elif _is_brand(tok):
            out.append(tok)
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)


def as_question(text):
    text = single_line(text)
    if not text:
        return text
    text = text[0].upper() + text[1:]
    if not text.endswith("?"):
        text += "?"
    return text


def as_sentence(text):
    text = single_line(text)
    if not text:
        return text
    text = text[0].upper() + text[1:]
    if not text.endswith((".", "!", "?")):
        text += "."
    return text


def load_rows():
    if not CSV_PATH.exists():
        fail(f"{CSV_PATH} not found")
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            fail(f"Missing required columns: {missing}")
        rows = []
        for i, row in enumerate(reader, start=2):
            if not row["repo_name"] or not row["repository_url"]:
                fail(f"CSV line {i}: repo_name and repository_url are required")
            rows.append(row)
        return rows


def group_by_domain(rows):
    grouped = {}
    seen = {}
    for row in rows:
        domains = [d.strip() for d in (row.get("navigation_domains") or "").split(";") if d.strip()]
        if not domains:
            domains = [row["primary_domain"].strip()]
        for d in domains:
            seen.setdefault(d, set())
            if row["repo_name"] in seen[d]:
                continue
            seen[d].add(row["repo_name"])
            grouped.setdefault(d, []).append(row)
    return grouped


def build_table(rows):
    rows = sorted(rows, key=lambda r: humanize_repo_name(r["repo_name"]))
    lines = [TABLE_HEADER]
    for row in rows:
        lines.append(
            f'| {escape_cell(humanize_repo_name(row["repo_name"]))} | '
            f'{escape_cell(as_question(row["helps_you_decide"]))} | '
            f'{escape_cell(as_sentence(row["primary_capability"]))} | '
            f'[Repository]({row["repository_url"].strip()}) |'
        )
    return "\n".join(lines)


def replace_section(text, filename, table):
    if SECTION_HEADING not in text:
        fail(f"{filename}: heading not found")
    pattern = re.compile(
        rf"({re.escape(SECTION_HEADING)})\n.*?(?=\n## |\Z)",
        re.DOTALL,
    )
    new_text, n = pattern.subn(lambda m: m.group(1) + "\n\n" + table + "\n", text, count=1)
    if n == 0:
        fail(f"{filename}: could not replace section")
    return new_text


def main():
    rows = load_rows()
    grouped = group_by_domain(rows)

    planned = {}

    for domain, domain_rows in grouped.items():
        md = CATALOG_DIR / f"{domain}.md"
        if not md.exists():
            fail(f"Missing catalog page: {md.name}")
        planned[md] = replace_section(
            md.read_text(encoding="utf-8"),
            md.name,
            build_table(domain_rows),
        )

    changed = []

    for md, new_text in planned.items():
        old = md.read_text(encoding="utf-8")
        if old != new_text:
            md.write_text(new_text, encoding="utf-8")
            changed.append(md.name)

    print(f"Updated {len(changed)} catalog file(s): {changed}")


if __name__ == "__main__":
    main()
