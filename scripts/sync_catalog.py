#!/usr/bin/env python3
"""Rebuild the Business Decision Toolbox catalog from repository READMEs.

The repository README is the only source of truth for each project. This script
does a full GitHub account scan every time it runs, extracts deterministic fields
from README sections, regenerates catalog/tool-catalog.csv, and replaces only the
generated block in this repository's README.md.
"""

from __future__ import annotations

import base64
import csv
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
CATALOG_PATH = ROOT / "catalog" / "tool-catalog.csv"

API_ROOT = "https://api.github.com"
START_MARKER = "<!-- TOOL_CATALOG_START -->"
END_MARKER = "<!-- TOOL_CATALOG_END -->"

CSV_COLUMNS = [
    "Repository",
    "Project Name",
    "Category",
    "Description",
    "Business Scenario",
    "Core Features",
    "Target Users",
    "Tags",
    "GitHub URL",
    "Default Branch",
]

SECTION_ALIASES = {
    "project_name": (
        "project title",
        "project name",
        "tool name",
    ),
    "description": (
        "project description",
        "description",
        "overview",
    ),
    "category": (
        "business category",
        "category",
        "primary category",
        "primary domain",
        "business domain",
    ),
    "business_scenario": (
        "business scenario",
        "business problem",
        "business use case",
        "use case",
        "why this exists",
    ),
    "core_features": (
        "core features",
        "features",
        "key features",
        "what this tool does",
        "what the workbook does",
        "what the tool does",
    ),
    "target_users": (
        "target users",
        "who this tool is for",
        "who this is for",
        "ideal users",
    ),
    "tags": (
        "tags",
        "keywords",
    ),
}

NON_TOOL_REPOSITORY_NAMES = {
    "about-me",
}


@dataclass(frozen=True)
class CatalogEntry:
    repository: str
    project_name: str
    category: str
    description: str
    business_scenario: str
    core_features: str
    target_users: str
    tags: str
    github_url: str
    default_branch: str

    def csv_row(self) -> dict[str, str]:
        return {
            "Repository": self.repository,
            "Project Name": self.project_name,
            "Category": self.category,
            "Description": self.description,
            "Business Scenario": self.business_scenario,
            "Core Features": self.core_features,
            "Target Users": self.target_users,
            "Tags": self.tags,
            "GitHub URL": self.github_url,
            "Default Branch": self.default_branch,
        }


def api_request(path: str, token: str | None) -> tuple[object, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "business-decision-toolbox-catalog-sync",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(f"{API_ROOT}{path}", headers=headers)
    last_error: URLError | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                response_headers = {key.lower(): value for key, value in response.headers.items()}
            return json.loads(payload), response_headers
        except HTTPError:
            raise
        except URLError as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def paginated(path: str, token: str | None) -> Iterable[object]:
    next_path: str | None = path
    while next_path:
        payload, headers = api_request(next_path, token)
        if not isinstance(payload, list):
            raise TypeError(f"Expected a list response from {next_path}")
        yield from payload
        next_path = parse_next_link(headers.get("link", ""))


def parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<https://api\.github\.com([^>]+)>", part)
        if match:
            return match.group(1)
    return None


def fetch_readme(owner: str, repo: str, token: str | None) -> str | None:
    path = f"/repos/{quote(owner)}/{quote(repo)}/readme"
    try:
        payload, _ = api_request(path, token)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    if not isinstance(payload, dict):
        raise TypeError(f"Unexpected README response for {owner}/{repo}")
    content = payload.get("content")
    encoding = payload.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        return None
    return base64.b64decode(content).decode("utf-8", errors="replace")


def normalize_heading(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[*_`~\[\]():#]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().casefold()


def extract_sections(markdown: str) -> dict[str, str]:
    heading_pattern = re.compile(r"^(#{2,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
    matches = list(heading_pattern.finditer(markdown))
    sections: dict[str, str] = {}

    for index, match in enumerate(matches):
        heading = normalize_heading(match.group(2))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.setdefault(heading, markdown[start:end].strip())

    return sections


def extract_h1(markdown: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*#*\s*$", markdown, flags=re.MULTILINE)
    return clean_text(match.group(1)) if match else ""


def extract_field(sections: dict[str, str], field: str) -> str:
    for alias in SECTION_ALIASES[field]:
        content = sections.get(alias)
        if content:
            return clean_text(content)
    return ""


def extract_tags(sections: dict[str, str]) -> str:
    raw = extract_field(sections, "tags")
    if not raw:
        return ""
    parts = re.split(r"[,;\n|]+", raw)
    cleaned = [clean_text(part) for part in parts]
    return "; ".join(part for part in cleaned if part)


def clean_text(markdown: str, *, max_length: int = 360) -> str:
    text = html.unescape(markdown)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"^[>#*\-\s]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"\s*\|\s*", " | ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def build_entry(repo: dict[str, object], readme: str) -> CatalogEntry:
    sections = extract_sections(readme)
    repo_name = str(repo["name"])
    project_name = extract_field(sections, "project_name") or extract_h1(readme)

    return CatalogEntry(
        repository=repo_name,
        project_name=project_name,
        category=extract_field(sections, "category"),
        description=extract_field(sections, "description"),
        business_scenario=extract_field(sections, "business_scenario"),
        core_features=extract_field(sections, "core_features"),
        target_users=extract_field(sections, "target_users"),
        tags=extract_tags(sections),
        github_url=str(repo["html_url"]),
        default_branch=str(repo.get("default_branch") or ""),
    )


def scan_repositories(owner: str, source_repository: str, token: str | None) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    path = f"/users/{quote(owner)}/repos?per_page=100&type=owner&sort=full_name"

    for repo_obj in paginated(path, token):
        if not isinstance(repo_obj, dict):
            continue
        repo_name = str(repo_obj.get("name") or "")
        full_name = str(repo_obj.get("full_name") or "")
        if not repo_name:
            continue
        if full_name.casefold() == source_repository.casefold():
            continue
        if repo_name.casefold() in NON_TOOL_REPOSITORY_NAMES:
            continue
        if repo_obj.get("archived") or repo_obj.get("disabled") or repo_obj.get("fork"):
            continue

        readme = fetch_readme(owner, repo_name, token)
        if readme is None:
            continue
        entries.append(build_entry(repo_obj, readme))

    return sorted(
        entries,
        key=lambda entry: (
            entry.category.casefold() or "zzzzzz",
            entry.project_name.casefold() or entry.repository.casefold(),
            entry.repository.casefold(),
        ),
    )


def write_csv(entries: list[CatalogEntry]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry.csv_row())


def markdown_escape(value: str) -> str:
    value = value.replace("\n", " ")
    value = value.replace("|", "\\|")
    return value


def build_readme_block(entries: list[CatalogEntry]) -> str:
    categories = sorted({entry.category for entry in entries if entry.category}, key=str.casefold)
    uncategorized = [entry for entry in entries if not entry.category]

    lines = [
        "_This section is generated from project README files. Do not edit it manually._",
        "",
        f"Total repositories scanned into catalog: **{len(entries)}**",
        f"Categories found: **{len(categories)}**",
        "",
    ]

    grouped_names = categories + (["Uncategorized"] if uncategorized else [])
    for category in grouped_names:
        group = [
            entry
            for entry in entries
            if (entry.category == category or (category == "Uncategorized" and not entry.category))
        ]
        lines.extend(
            [
                f"### {category}",
                "",
                "| Project | Business Scenario | Description | Tags | Repository |",
                "|---|---|---|---|---|",
            ]
        )
        for entry in group:
            project = markdown_escape(entry.project_name or entry.repository)
            scenario = markdown_escape(entry.business_scenario)
            description = markdown_escape(entry.description)
            tags = markdown_escape(entry.tags)
            lines.append(
                f"| {project} | {scenario} | {description} | {tags} | "
                f"[Repository]({entry.github_url}) |"
            )
        lines.append("")

    if not entries:
        lines.append("No repositories with readable README files were found.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def update_readme(entries: list[CatalogEntry]) -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    generated = build_readme_block(entries)
    replacement = f"{START_MARKER}\n{generated}{END_MARKER}"

    if START_MARKER in readme and END_MARKER in readme:
        pattern = re.compile(
            rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
            flags=re.DOTALL,
        )
        updated = pattern.sub(replacement, readme)
    else:
        insertion = (
            "## Repository Catalog\n\n"
            f"{replacement}\n\n"
            "## Catalog Automation\n\n"
            "Project READMEs are the single source of truth for this directory. "
            "`catalog/tool-catalog.csv` and the generated catalog block above are "
            "rebuilt by `scripts/sync_catalog.py`; do not edit generated output manually.\n\n"
            "For a repository to populate the catalog completely, its README should use "
            "these deterministic section headings: `Project Description`, "
            "`Business Category`, `Business Scenario`, `Core Features`, "
            "`Target Users`, and `Tags`. Missing sections are left blank instead of "
            "being inferred.\n\n"
        )
        anchor = "\n## Methodology Context\n"
        if anchor in readme:
            updated = readme.replace(anchor, f"\n{insertion}{anchor}", 1)
        else:
            updated = readme.rstrip() + "\n\n" + insertion

    README_PATH.write_text(updated, encoding="utf-8", newline="\n")


def main() -> int:
    source_repository = os.environ.get("CATALOG_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY") or ""
    owner = os.environ.get("CATALOG_OWNER") or (source_repository.split("/", 1)[0] if "/" in source_repository else "")
    token = os.environ.get("CATALOG_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if not owner:
        print("CATALOG_OWNER or GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 2
    if not source_repository:
        source_repository = f"{owner}/business-decision-toolbox"

    entries = scan_repositories(owner, source_repository, token)
    write_csv(entries)
    update_readme(entries)
    print(f"Catalog rebuilt from {len(entries)} repositories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
