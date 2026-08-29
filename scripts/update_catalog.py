#!/usr/bin/env python3
"""update_catalog.py -- Daily upstream scan that keeps catalog/tool-catalog.csv fresh.

This is the automated port of the WorkBuddy "update GitHub catalog" four-phase
workflow. It runs inside GitHub Actions (see .github/workflows/update-catalog-daily.yml)
and is completely independent of the existing scripts/sync_catalog.py, which
regenerates the catalog/*.md pages AFTER this script updates the CSV. The two
form a chain: update-catalog-daily commits a new CSV -> the existing
"sync-catalog.yml" workflow picks up the CSV change and regenerates the md pages.

Pipeline:
  Phase 1  List all public non-fork, non-archived repos of OWNER.
  Phase 2  Diff against catalog/tool-catalog.csv:
             - in API list but not in CSV   -> NEW    (goes to Phase 3)
             - in CSV but not in API list   -> fetch /repos/OWNER/<old>:
                 200 with a different `name` -> RENAMED (keep the row, update
                 repo_name/repository_url only); 404 -> DELETED (drop the row).
               Note: GitHub search does not surface old names of renamed repos,
               which is exactly why the redirect probe above is required before
               declaring anything deleted.
  Phase 3  For each NEW repo: fetch README, classify primary_domain by keyword
           hit counts (an LLM tie-break resolves near-draws, matching the
           project rule "top count is 0 or the top two are within 2 -> judge
           from the README as a whole"), then generate "Helps You Decide" and
           "Primary Capability" via GitHub Models. An LLM failure aborts the
           whole run -- placeholder rows must never land in the catalog.
  Phase 4  If anything changed: archive a dated snapshot to
           catalog/history/tool-catalog-YYYY-MM-DD.csv and overwrite
           catalog/tool-catalog.csv. Unchanged days write nothing at all, so
           the history folder records actual catalog changes, not daily noise.

CSV conventions (identical to the manual workflow):
  UTF-8 without BOM, LF line endings, header row
  repo_name,primary_domain,navigation_domains,Helps You Decide,Primary Capability,repository_url
  Existing rows are NEVER rewritten (renames only update the name/url pair),
  so hand-curated wording in the CSV always survives automated runs.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

OWNER = "HyVoid"

# Repos that must never enter the catalog. About-me is a personal intro page,
# business-decision-toolbox hosts this catalog itself, and
# Affiliate-Program-Collaborations is an affiliate/collaboration program page
# rather than a decision tool. Extend this set if similar non-tool repos appear.
EXCLUDED_REPOS = {
    "About-me",
    "business-decision-toolbox",
    "Affiliate-Program-Collaborations",
}

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = ROOT / "catalog"
HISTORY_DIR = CATALOG_DIR / "history"
CSV_PATH = CATALOG_DIR / "tool-catalog.csv"

CSV_COLUMNS = [
    "repo_name",
    "primary_domain",
    "navigation_domains",
    "Helps You Decide",
    "Primary Capability",
    "repository_url",
]

API_BASE = "https://api.github.com"

# LLM inference for new-repo classification. Any OpenAI-compatible
# /chat/completions endpoint works (DeepSeek, OpenRouter, Groq, Google AI
# Studio, Azure Foundry, ...). GitHub Models was retired on 2026-07-30 and
# can no longer be used here. Default: DeepSeek (cheap, JSON mode supported).
# Configure via environment variables in the workflow; see
# .github/workflows/update-catalog-daily.yml.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
LLM_CHAT_URL = f"{LLM_BASE_URL}/chat/completions"

# Timeout / retry tuning for all HTTP calls.
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

# --------------------------------------------------------------------------
# Domain keyword tables (from the project's domain_keywords reference)
# --------------------------------------------------------------------------

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "profitability": [
        "cash flow", "margin", "revenue", "valuation", "budget", "loan",
        "currency", "investment", "acquisition model", "unitization", "pricing",
        "profit", "nav", "net asset value", "return on", "ebitda", "forecasting",
        "liquidity",
    ],
    "inventory": [
        "inventory", "sku", "stockout", "reorder", "replenish", "cargo",
        "warehouse", "safety stock", "cin7", "stock level", "purchase order",
        "demand signal", "pre-order", "return rate", "allocation", "assortment",
        "stock exposure",
    ],
    "marketing": [
        "campaign", "attribution", "crm", "engagement", "ad spend", "paid media",
        "marketing", "membership", "channel", "conversion", "keyword", "ctr",
        "cvr", "acos", "ppc", "amazon seller", "shopify", "tiktok",
    ],
    "operations": [
        "dispatch", "schedule", "field service", "operations", "workflow",
        "kpi dashboard", "audit", "project time", "project cost", "wedding",
        "restaurant", "lawn care", "order tracking", "settlement tracking",
        "construction management", "progress billing", "change order",
        "performance review",
    ],
    "compliance": [
        "vat", "tax", "compliance", "aviation", "iif", "regulation", "filing",
        "recken", "regulatory", "jurisdiction", "filing return", "declaration",
    ],
    "engineering": [
        "shaft", "din 743", "fatigue", "structural", "construction estimat",
        "aia", "construction cost", "shaft calculation", "engineering decision",
        "tender", "bid preparation", "assembly",
    ],
}

# data-architecture is never a primary_domain; it is only appended to
# navigation_domains when the README shows data-unification intent.
DATA_ARCH_FEATURES = [
    "multi-source", "unified data", "cross-system", "data pipeline",
    "data normalization", "data standardization", "data cleaning",
    "single source of truth", "unified fact table", "data integration",
    "standardize", "standardized",
]

VALID_DOMAINS = list(DOMAIN_KEYWORDS.keys())

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"update_catalog.py: {message}", file=sys.stderr)
    raise SystemExit(1)


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        fail("GITHUB_TOKEN is not set (GitHub Actions provides it automatically)")
    return token


def llm_api_key() -> str:
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        fail(
            "LLM_API_KEY is not set. A new repository needs LLM classification, "
            "which requires an OpenAI-compatible inference provider (GitHub "
            "Models was retired 2026-07-30). Add LLM_API_KEY (and optionally "
            "LLM_BASE_URL / LLM_MODEL) as a repository secret -- see the "
            "comments in .github/workflows/update-catalog-daily.yml. Days with "
            "no new repositories run fine without it."
        )
    return key


def http_json(url: str, token: str, *, method: str = "GET", payload: dict | None = None, github_api: bool = True) -> tuple[int, object]:
    """Perform an HTTP request with retries; return (status, parsed_json_or_None).

    4xx responses are returned as-is (callers branch on the status); network
    errors and 5xx are retried with backoff. github_api=False drops the
    GitHub-specific headers so the same helper also works for the
    OpenAI-compatible LLM endpoint.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        if github_api:
            request.add_header("Accept", "application/vnd.github+json")
            request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                raw = error.read().decode("utf-8", errors="replace") if error.fp else ""
                try:
                    return error.code, (json.loads(raw) if raw else None)
                except json.JSONDecodeError:
                    return error.code, None
            last_error = error  # 5xx: retry
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error  # network: retry
        if attempt < HTTP_RETRIES:
            time.sleep(2 * attempt)
    raise RuntimeError(f"HTTP {method} {url} failed after {HTTP_RETRIES} attempts: {last_error}")


# --------------------------------------------------------------------------
# Phase 1 -- list repositories
# --------------------------------------------------------------------------


def list_repos(token: str) -> list[str]:
    """Return all public non-fork, non-archived repo names for OWNER."""
    names: list[str] = []
    page = 1
    while True:
        status, data = http_json(
            f"{API_BASE}/users/{OWNER}/repos?per_page=100&page={page}&type=owner",
            token,
        )
        if status != 200 or not isinstance(data, list):
            fail(f"could not list repos for {OWNER} (HTTP {status})")
        if not data:
            break
        for repo in data:
            if repo.get("fork") or repo.get("archived") or repo.get("private"):
                continue
            names.append(repo["name"])
        if len(data) < 100:
            break
        page += 1
    return sorted(set(names))


# --------------------------------------------------------------------------
# Phase 2 -- rename/delete detection
# --------------------------------------------------------------------------


def probe_missing_repo(repo_name: str, token: str) -> str | None:
    """For a CSV row whose repo is absent from the API listing, decide whether
    the repo was renamed or deleted.

    Returns the NEW name for a rename, or None for a confirmed deletion.
    A 200 whose `name` equals the old name means the listing was stale --
    treat that as "still exists" and keep the row unchanged.
    """
    status, data = http_json(f"{API_BASE}/repos/{OWNER}/{repo_name}", token)
    if status == 404:
        return None  # confirmed deleted
    if status == 200 and isinstance(data, dict):
        current = data.get("name", "")
        if current and current != repo_name:
            return current  # renamed: GitHub followed the redirect for us
        return repo_name  # still exists under the same name (stale listing)
    fail(f"unexpected HTTP {status} while probing {repo_name}")


# --------------------------------------------------------------------------
# Phase 3 -- README classification + LLM semantic fields
# --------------------------------------------------------------------------


def fetch_readme(repo_name: str, token: str) -> str:
    status, data = http_json(
        f"{API_BASE}/repos/{OWNER}/{repo_name}/readme", token
    )
    if status != 200 or not isinstance(data, dict):
        fail(f"could not fetch README for {repo_name} (HTTP {status})")
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (KeyError, ValueError) as error:
        fail(f"malformed README payload for {repo_name}: {error}")


def extract_sections(readme: str) -> dict[str, str]:
    """Pull out the three semantic blocks the catalog rules rely on:
    header_block (H1 -> first --- or ## heading), why_text ("Why I Built This")
    and decision_text ("What It Helps You Track"). Falls back to the README
    opening when a section is missing."""
    lines = readme.splitlines()

    def find_heading(pattern: str) -> int:
        compiled = re.compile(pattern, re.IGNORECASE)
        for index, line in enumerate(lines):
            if line.startswith("#") and compiled.search(line):
                return index
        return -1

    header_end = len(lines)
    for index, line in enumerate(lines):
        if index == 0:
            continue
        if line.strip() == "---" or (line.startswith("#") and not line.startswith("#!") ):
            header_end = index
            break
    header_block = "\n".join(lines[:header_end]).strip()

    def section_between(start_pattern: str) -> str:
        start = find_heading(start_pattern)
        if start < 0:
            return ""
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].startswith("#"):
                end = index
                break
        return "\n".join(lines[start + 1 : end]).strip()

    why_text = section_between(r"why (i|we) built")
    decision_text = section_between(r"what it helps you (track|decide|manage)")

    return {
        "header_block": header_block[:2000],
        "why_text": why_text[:2000],
        "decision_text": decision_text[:2000],
    }


def count_domain_hits(readme_lower: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        hits[domain] = sum(readme_lower.count(keyword) for keyword in keywords)
    return hits


def detect_data_architecture(readme_lower: str) -> bool:
    return any(feature in readme_lower for feature in DATA_ARCH_FEATURES)


LLM_SYSTEM_PROMPT = """You maintain a business-decision-tool catalog. For each repository you are given, you write exactly three things, following these field rules strictly:

1. "helps_you_decide": one top-level business question a manager is actually asking when they open this tool. English, ends with "?". Synthesized from the README's motivation and tracking sections -- never copied verbatim, never a list, never a sub-question.

2. "primary_capability": one sentence starting with an imperative verb (Forecast, Measure, Compare, Separate, Evaluate, Calculate, Connect, Integrate, Apply, Identify, ...). English. Generalizes the tool's core function from its header/tagline block -- not a question, not a verbatim tagline copy.

3. "primary_domain": exactly one of profitability, inventory, marketing, operations, compliance, engineering -- the business area this tool primarily serves. Use "data-architecture" never; it is not a primary domain.

Respond with a single JSON object with exactly those three keys and string values."""


def llm_classify(
    repo_name: str,
    sections: dict[str, str],
    readme_opening: str,
    api_key: str,
) -> dict[str, str]:
    """Ask the configured OpenAI-compatible endpoint for the two semantic
    fields plus a domain vote."""
    user_content = (
        f"Repository: {repo_name}\n\n"
        f"README header block:\n{sections['header_block'] or readme_opening}\n\n"
        f"Why I built this (if present):\n{sections['why_text'] or '(absent)'}\n\n"
        f"What it helps you track (if present):\n{sections['decision_text'] or '(absent)'}\n\n"
        "README opening (fallback context):\n"
        f"{readme_opening}\n\n"
        'Return JSON: {"helps_you_decide": "...", "primary_capability": "...", "primary_domain": "..."}'
    )
    status, data = http_json(
        LLM_CHAT_URL,
        api_key,
        method="POST",
        github_api=False,
        payload={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 400,
        },
    )
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(
            f"LLM endpoint returned HTTP {status} for {repo_name}: {json.dumps(data)[:300]}"
        )
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    result = {
        "helps_you_decide": str(parsed["helps_you_decide"]).strip(),
        "primary_capability": str(parsed["primary_capability"]).strip(),
        "primary_domain": str(parsed["primary_domain"]).strip().lower(),
    }
    if result["primary_domain"] not in VALID_DOMAINS:
        raise RuntimeError(
            f"LLM returned invalid primary_domain {result['primary_domain']!r} for {repo_name}"
        )
    for field in ("helps_you_decide", "primary_capability"):
        if not result[field]:
            raise RuntimeError(f"LLM returned empty {field} for {repo_name}")
    return result


def build_new_row(
    repo_name: str,
    token: str,
    *,
    use_llm: bool,
) -> dict[str, str]:
    """Full Phase 3 for one new repo: README -> domain -> semantic fields."""
    readme = fetch_readme(repo_name, token)
    readme_lower = readme.lower()
    sections = extract_sections(readme)
    readme_opening = readme.strip()[:1500]

    hits = count_domain_hits(readme_lower)
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    top_domain, top_hits = ranked[0]

    # Domain decision. Keyword counting is the heuristic; the LLM vote is the
    # automated equivalent of the manual workflow's "judge from the README as
    # a whole" tie-break. When both agree, the keyword count corroborates the
    # choice; when they disagree, the LLM wins -- word-frequency counts are
    # systematically biased (e.g. "pricing"/"currency" dominate construction
    # estimating READMEs that are clearly engineering tools).
    if use_llm:
        # Lazy key lookup: days without new repos never touch the LLM at all,
        # so the workflow runs fine before LLM_API_KEY has been configured.
        llm_result = llm_classify(repo_name, sections, readme_opening, llm_api_key())
        if llm_result["primary_domain"] == top_domain and top_hits > 0:
            primary_domain = top_domain
            domain_source = f"keywords+llm({top_hits} hits)"
        else:
            primary_domain = llm_result["primary_domain"]
            domain_source = f"llm-judgment(keywords top: {top_domain} {top_hits} hits)"
    else:
        llm_result = {
            "helps_you_decide": "TBD (LLM disabled)",
            "primary_capability": "TBD (LLM disabled).",
            "primary_domain": "",
        }
        if top_hits == 0:
            fail(f"cannot classify {repo_name}: no keyword hits and LLM disabled")
        primary_domain = top_domain
        domain_source = f"keywords({top_hits} hits)"

    primary_hits = hits.get(primary_domain, 0)

    # navigation_domains: primary first, then any other business domain whose
    # keyword hits reach at least half of the primary's (minimum 2), then
    # data-architecture when data-unification intent is detected.
    navigation = [primary_domain]
    for domain, count in ranked:
        if domain == primary_domain or count < 2:
            continue
        if count >= max(2, (primary_hits + 1) // 2):
            navigation.append(domain)
    if detect_data_architecture(readme_lower) and "data-architecture" not in navigation:
        navigation.append("data-architecture")

    log(
        f"  classified {repo_name}: {primary_domain} [{domain_source}], "
        f"nav={'/'.join(navigation)}"
    )
    return {
        "repo_name": repo_name,
        "primary_domain": primary_domain,
        "navigation_domains": ";".join(navigation),
        "Helps You Decide": llm_result["helps_you_decide"],
        "Primary Capability": llm_result["primary_capability"],
        "repository_url": f"https://github.com/{OWNER}/{repo_name}",
    }


# --------------------------------------------------------------------------
# Phase 4 -- CSV load / write
# --------------------------------------------------------------------------


def load_csv() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        fail(f"{CSV_PATH} not found")
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [column for column in CSV_COLUMNS if column not in fieldnames]
        if missing:
            fail(f"{CSV_PATH.name} is missing column(s): {missing}")
        return [dict(row) for row in reader]


def write_csv(rows: list[dict[str, str]], *, dry_run: bool) -> None:
    content_lines = [",".join(CSV_COLUMNS)]
    for row in rows:
        values = []
        for column in CSV_COLUMNS:
            value = (row.get(column) or "").replace("\r", " ").replace("\n", " ").strip()
            if any(character in value for character in (",", '"')):
                value = '"' + value.replace('"', '""') + '"'
            values.append(value)
        content_lines.append(",".join(values))
    content = "\n".join(content_lines) + "\n"

    if dry_run:
        log("(dry-run) would write catalog/tool-catalog.csv and a history snapshot")
        return

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = HISTORY_DIR / f"tool-catalog-{date.today().isoformat()}.csv"
    CSV_PATH.write_text(content, encoding="utf-8", newline="")
    snapshot.write_text(content, encoding="utf-8", newline="")
    log(f"wrote {CSV_PATH.relative_to(ROOT)} and snapshot {snapshot.relative_to(ROOT)}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the detected changes without writing any file",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip GitHub Models calls (mechanical testing only; new rows get TBD text)",
    )
    args = parser.parse_args()

    token = github_token()

    # ---- Phase 1
    all_repos = list_repos(token)
    catalog_repos = [name for name in all_repos if name not in EXCLUDED_REPOS]
    log(f"Phase 1: {len(all_repos)} public non-fork repos, "
        f"{len(catalog_repos)} after exclusions ({sorted(EXCLUDED_REPOS & set(all_repos))})")

    # ---- Phase 2
    rows = load_csv()
    csv_names = [row["repo_name"].strip() for row in rows]
    known = set(csv_names)
    listed = set(catalog_repos)

    new_repos = sorted(listed - known)
    missing = [name for name in csv_names if name not in listed]
    log(f"Phase 2: CSV has {len(rows)} rows; {len(new_repos)} new repo(s); "
        f"{len(missing)} missing repo(s) to probe for rename/deletion")

    renamed: dict[str, str] = {}
    deleted: list[str] = []
    for old_name in missing:
        probe = probe_missing_repo(old_name, token)
        if probe is None:
            deleted.append(old_name)
            log(f"  DELETED: {old_name}")
        elif probe == old_name:
            log(f"  UNCHANGED: {old_name} (stale listing, kept as-is)")
        else:
            renamed[old_name] = probe
            log(f"  RENAMED: {old_name} -> {probe}")

    # A rename whose new name also appears in new_repos must not be
    # double-processed: treat it as a rename, not a brand-new repo.
    new_repos = [name for name in new_repos if name not in renamed.values()]
    log(f"Phase 3: {len(new_repos)} repo(s) need full classification "
        f"({len(renamed)} handled as rename(s))")

    # ---- Phase 3 (new repos only; renames keep their curated fields)
    new_rows: list[dict[str, str]] = []
    for repo_name in new_repos:
        row = build_new_row(repo_name, token, use_llm=not args.no_llm)
        new_rows.append(row)

    # ---- Phase 4: assemble the updated row list, preserving existing order
    updated_rows: list[dict[str, str]] = []
    for row in rows:
        name = row["repo_name"].strip()
        if name in deleted:
            continue
        if name in renamed:
            row = dict(row)
            row["repo_name"] = renamed[name]
            row["repository_url"] = f"https://github.com/{OWNER}/{renamed[name]}"
        updated_rows.append(row)
    updated_rows.extend(new_rows)

    # Sanity check mirroring the manual workflow rule:
    # catalog rows must equal catalog-worthy repos.
    expected = len(listed)
    actual = len(updated_rows)
    if actual != expected:
        fail(
            f"row-count sanity check failed: {actual} rows in CSV vs "
            f"{expected} catalog-worthy repos; aborting without writing"
        )

    changed = bool(new_rows or renamed or deleted)
    if not changed:
        log("No changes detected; catalog is already up to date.")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path and not args.dry_run:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Tool catalog daily update\n\nNo changes.\n")
        return 0

    log(
        f"Phase 4: +{len(new_rows)} added, {len(renamed)} renamed, "
        f"{len(deleted)} deleted -> {actual} total rows"
    )
    for row in new_rows:
        log(f"  + {row['repo_name']} ({row['primary_domain']})")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and not args.dry_run:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("### Tool catalog daily update\n\n")
            for row in new_rows:
                handle.write(
                    f"- **Added** `{row['repo_name']}` ({row['primary_domain']})\n"
                )
            for old, new in renamed.items():
                handle.write(f"- **Renamed** `{old}` -> `{new}`\n")
            for name in deleted:
                handle.write(f"- **Deleted** `{name}`\n")

    write_csv(updated_rows, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
