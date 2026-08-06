#!/usr/bin/env python3
"""PR labeler: compute size, risk, template-field, and domain labels for PRs.

Inputs come from environment variables set by the calling workflow:
  GITHUB_REPOSITORY  e.g. "owner/repo" (always set on Actions)
  PR_NUMBER          "" (event mode), "all" (backfill), or "<number>"
  DRY_RUN            "true" or "false"
  EVENT_PR_NUMBER    PR number from `pull_request` event (if any), else ""
  REASONS_TARGET     "comment" (default) or "body" -- where to publish the
                     domain-reason section (see step 7)

The script processes each PR by:
  1. Fetching additions, deletions, body, and current labels from the GitHub API.
  2. Computing the size bucket from additions+deletions.
  3. Parsing the Bugbot CURSOR_SUMMARY block for a risk level.
  4. Parsing the PR template fields for `urgent` and `high complexity`.
     Two formats are supported:
       - Current: ``- **Urgent** (...): yes`` / ``: no``
       - Legacy:  ``- [x] **Urgent** ...`` / ``- [ ] **Urgent** ...``
     The current format is preferred; the legacy format is matched as a
     fallback so PRs opened before the template change keep working until
     the queue rolls over.
  5. Matching changed files against CODEOWNERS to apply domain/* labels.
  6. Reconciling with current labels and applying adds/removes via `gh pr edit`.
  7. Publishing a managed "why these teams were requested for review" section
     that explains every CODEOWNERS-requested team (including the eng-leads
     catch-all) with the files/patterns that matched. This is separate from the
     domain/* labels. REASONS_TARGET picks where it goes:
       - "comment" (default): a single sticky PR comment carrying the
         DOMAIN_REVIEW_REASONS:START/END markers, created once, edited in place
         afterwards, and deleted if the PR no longer touches any owned files.
       - "body": a region of the PR description delimited by the same markers,
         rewritten in place and only when the body actually changes (quiet, no
         notifications).

For backfill mode (PR_NUMBER == "all"), per-PR failures are logged but do not
abort the run unless at least MIN_FAILURES_TO_FAIL PRs fail and they exceed
FAILURE_RATE_THRESHOLD of the run; a single failure never fails the run.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch

SIZE_LABELS = ["size/XS", "size/S", "size/M", "size/L", "size/XL"]
RISK_LABELS = ["risk/low", "risk/medium", "risk/high"]
URGENT_LABEL = "review/urgent"
COMPLEXITY_LABEL = "complexity/high"

URGENT_KEYWORD = "urgent"
COMPLEXITY_KEYWORD = "high complexity"

CURSOR_SUMMARY_MARKER = "<!-- CURSOR_SUMMARY -->"
RISK_REGEX = re.compile(r"\*\*(\w+)\s+Risk\*\*", re.IGNORECASE)
RISK_MAP = {
    "low": "risk/low",
    "medium": "risk/medium",
    "high": "risk/high",
}
# Conservative fallback for unmapped Bugbot levels (e.g., "Critical", "Minimal").
RISK_FALLBACK = "risk/high"

DOMAIN_LABEL_PREFIX = "domain/"
KNOWN_DOMAIN_SLUGS = frozenset(
    [
        "scanning",
        "findings",
        "integrations",
        "platform",
        "frontend",
        "infra",
        "database",
    ]
)

# Managed section written into the PR body explaining why each team was
# requested for review. Delimited by these markers so the labeler can rewrite
# its own region in place without disturbing the rest of the description (the
# same technique Bugbot uses with CURSOR_SUMMARY).
DOMAIN_REASONS_START = "<!-- DOMAIN_REVIEW_REASONS:START -->"
DOMAIN_REASONS_END = "<!-- DOMAIN_REVIEW_REASONS:END -->"

# One-line charters, sourced from the Cross-Repo Ownership Redesign plan, so the
# reason explains *what* the team owns rather than just listing files. Covers the
# 7 KNOWN_DOMAIN_SLUGS plus the eng-leads catch-all. Owners without an entry here
# are still rendered (raw slug, no charter). Insertion order also defines the
# display order of known teams (see render_domain_reasons) -- do not rely on set
# order, which varies per process under hash randomization.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "scanning": "scan engine, job control, reverification",
    "integrations": "sources, detectors, and analyzers",
    "findings": "secrets triage, dashboard, issues, reporting, notifications",
    "platform": "auth, RBAC, API keys, audit logs, observability, dev tooling",
    "frontend": "shared frontend/React code, design system, view API contracts",
    "infra": "infrastructure, CI, and root build/deploy configs",
    "database": "DB models, migrations, signals, and Go DB infrastructure",
    "eng-leads": "engineering leads; default reviewers for files no domain team owns",
}

# Max files listed per team before truncating with an "...and N more" line.
DOMAIN_REASON_FILE_CAP = 10

# `gh pr view --json files` reads a GraphQL connection that gh requests with
# `first: 100` and never pages, so a PR touching more files hands back a
# silently truncated list -- no warning, no total count. Domain labeling is the
# only consumer that cares, and truncation is not a harmless approximation
# there: CODEOWNERS is last-match-wins, and the ownerless `go.mod`/`go.sum`/
# `/vendor/` rules that let Renovate automerge sort near the end of the file, so
# a large dependency bump fills the whole window with unowned vendor churn and
# buries the handful of first-party paths a team actually owns. The PR then
# looks entirely unowned: no domain/* labels and no reason comment, even while
# GitHub itself requests the owning teams as reviewers.
#
# A list exactly this long is the only available signal that more may follow, so
# it triggers a full re-fetch. Anything shorter is provably complete and keeps
# the single-call fast path (~98% of open PRs in practice).
GH_PR_VIEW_FILE_CAP = 100

# Where to publish the reason section. "comment" (the default) maintains a
# single sticky PR comment (one notification when first created, silent edits
# after); "body" edits a managed region of the PR description (quiet, no
# notifications). Selected per repo via the reusable workflow's
# `reasons_target` input.
REASONS_TARGET_BODY = "body"
REASONS_TARGET_COMMENT = "comment"

# Failure-rate gate for backfill mode. A single failure never fails the run, so
# event mode (one target) tolerates one transient error even if gh() retries are
# exhausted; backfill still fails when 2+ PRs fail and the rate exceeds the
# threshold.
MIN_FAILURES_TO_FAIL = 2
FAILURE_RATE_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# CODEOWNERS parsing (last-match-wins per file, union across all files)
# ---------------------------------------------------------------------------

CodeownersRule = tuple[str, list[str]]  # (pattern, [team_slugs])


def parse_codeowners(text: str) -> list[CodeownersRule]:
    """Parse CODEOWNERS text into an ordered list of (pattern, teams) rules."""
    rules: list[CodeownersRule] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        pattern = tokens[0]
        slugs: list[str] = []
        for owner in tokens[1:]:
            # @org/team -> team (lowercased)
            if "/" in owner:
                slugs.append(owner.rsplit("/", 1)[1].lower())
            else:
                slugs.append(owner.lstrip("@").lower())
        rules.append((pattern, slugs))
    return rules


def _codeowners_match(pattern: str, filepath: str) -> bool:
    """Test whether a CODEOWNERS pattern matches a file path.

    Implements GitHub's CODEOWNERS matching rules:
    - ``*`` alone matches everything.
    - A pattern starting with ``/`` is anchored to the repo root; the
      leading ``/`` is stripped before matching.
    - A pattern ending with ``/`` matches everything under that directory.
    - A pattern containing an internal ``/`` (after stripping leading ``/``)
      is implicitly anchored to the repo root.
    - A pattern with no ``/`` at all matches by basename at any depth.
    - A single ``*`` does not cross directory boundaries (unlike fnmatch);
      use ``**`` to match across directories.
    """
    if pattern == "*":
        return True

    anchored = pattern.startswith("/")
    p = pattern.lstrip("/")

    # Check for internal slash *before* appending ** for trailing-slash dirs.
    # A trailing-only slash does not anchor; only leading or internal slashes do.
    has_internal_slash = "/" in p.rstrip("/")

    if p.endswith("/"):
        p += "**"

    if anchored or has_internal_slash:
        return _gitignore_match(p, filepath)

    if "/" not in p:
        # No slash at all: match against basename at any depth.
        basename = filepath.rsplit("/", 1)[-1]
        return fnmatch(basename, p)

    # Trailing-slash-only dir (e.g. "vendor/") with no anchoring:
    # match at any depth by prepending **/.
    return _gitignore_match("**/" + p, filepath)


def _gitignore_match(pattern: str, filepath: str) -> bool:
    """Match a pattern against a filepath where ``*`` does not cross ``/``.

    Splits both pattern and path on ``/`` and matches segment-by-segment.
    ``**`` matches zero or more directory segments.
    """
    return _segments_match(pattern.split("/"), filepath.split("/"))


def _segments_match(pat_parts: list[str], path_parts: list[str]) -> bool:
    if not pat_parts:
        return not path_parts
    if pat_parts[0] == "**":
        rest = pat_parts[1:]
        # ** matches zero or more segments
        for i in range(len(path_parts) + 1):
            if _segments_match(rest, path_parts[i:]):
                return True
        return False
    if not path_parts:
        return False
    if fnmatch(path_parts[0], pat_parts[0]):
        return _segments_match(pat_parts[1:], path_parts[1:])
    return False


def domain_reasons(
    rules: list[CodeownersRule], changed_files: list[str]
) -> dict[str, list[tuple[str, str]]]:
    """Map each owning team slug to the files (and pattern) that requested it.

    Applies GitHub's last-match-wins semantics: each file is attributed to the
    owners of the *last* CODEOWNERS rule it matches. For every such
    (filepath, pattern) we record an entry under each owning slug, so the caller
    can both apply domain labels (``set(domain_reasons(...))``) and explain
    exactly why a team was added as a reviewer.

    Files whose last match has no owner (empty owner list, e.g. lock files or
    ``vendor/`` for Renovate) contribute nothing -- consistent with GitHub not
    requesting a reviewer for them.
    """
    reasons: dict[str, list[tuple[str, str]]] = {}
    for filepath in changed_files:
        matched_pattern: str | None = None
        matched_slugs: list[str] = []
        for pattern, slugs in rules:
            if _codeowners_match(pattern, filepath):
                matched_pattern = pattern
                matched_slugs = slugs
        if matched_pattern is None:
            continue
        for slug in matched_slugs:
            reasons.setdefault(slug, []).append((filepath, matched_pattern))
    return reasons


def _ordered_reason_slugs(slugs: list[str]) -> list[str]:
    """Return slugs in deterministic display order.

    Known teams appear first in ``DOMAIN_DESCRIPTIONS`` insertion order (which
    puts the eng-leads catch-all last), followed by any remaining slugs sorted
    alphabetically. Iterating a set (e.g. ``KNOWN_DOMAIN_SLUGS``) here would be
    non-deterministic under hash randomization and cause spurious body rewrites.
    """
    present = set(slugs)
    known = [s for s in DOMAIN_DESCRIPTIONS if s in present]
    rest = sorted(s for s in present if s not in DOMAIN_DESCRIPTIONS)
    return known + rest


def _code_span(text: str) -> str:
    """Wrap text in a backtick code span, neutralizing embedded backticks.

    GitHub sanitizes HTML, so this is cosmetic hardening to keep a pathological
    filename from breaking out of the span rather than a security control.
    """
    return f"`{text.replace('`', '')}`"


def render_domain_reasons(
    reasons: dict[str, list[tuple[str, str]]],
    codeowners_url: str | None = None,
    leading_rule: bool = True,
) -> str:
    """Render the managed reason section, or ``""`` when there are no owners.

    Explains every team requested for review (not just the labeled domains):
    each team is shown by its raw CODEOWNERS slug so it matches GitHub's
    "Reviewers" sidebar, with a short charter when one is known, followed by the
    files (and matching pattern) that triggered it. Output is deterministic so
    the caller can skip writing when nothing changed.

    Two-part layout: a visible ``> [!IMPORTANT]`` callout (headline, team count,
    provenance) followed by a default-collapsed ``<details>`` holding the
    per-team file/pattern breakdown, so the detail is one click away rather than
    always-on clutter. The callout stays *outside* the ``<details>`` because
    GitHub does not render alert callouts nested inside a ``<details>`` block --
    they degrade to a plain blockquote.

    ``leading_rule`` prepends a horizontal rule to visually separate the block
    from the author's text above it -- useful in the PR body, but noise in a
    standalone comment (where nothing precedes it), so comment mode passes False.
    """
    if not reasons:
        return ""

    codeowners = f"[CODEOWNERS]({codeowners_url})" if codeowners_url else "CODEOWNERS"
    ordered = _ordered_reason_slugs(list(reasons))
    count = len(ordered)
    teams = "team" if count == 1 else "teams"
    owns = "owns" if count == 1 else "own"

    lines = [DOMAIN_REASONS_START]
    if leading_rule:
        lines += ["---", ""]
    lines += [
        "> [!IMPORTANT]",
        "> **Why these teams were requested for review**",
        f"> {count} {teams} {owns} some of the files changed here, per {codeowners}. "
        "Expand for the files that matched.",
        "> <sub>Maintained automatically by the PR labeler.</sub>",
        "",
        "<details>",
        f"<summary>Which teams and files ({count})</summary>",
        "",
    ]
    for slug in ordered:
        charter = DOMAIN_DESCRIPTIONS.get(slug)
        heading = f"- **{slug}**"
        if charter:
            heading += f" - {charter}"
        lines.append(heading)
        files = sorted(reasons[slug])
        for filepath, pattern in files[:DOMAIN_REASON_FILE_CAP]:
            lines.append(f"  - {_code_span(filepath)} (matched {_code_span(pattern)})")
        remaining = len(files) - DOMAIN_REASON_FILE_CAP
        if remaining > 0:
            lines.append(f"  - ...and {remaining} more")
    lines += ["", "</details>", DOMAIN_REASONS_END]
    return "\n".join(lines)


def upsert_managed_section(body: str, section: str, start: str, end: str) -> str:
    """Insert, replace, or remove a marker-delimited section in ``body``.

    - Both markers present (``start`` before ``end``): replace the inclusive
      region with ``section`` (or remove it, plus the preceding blank
      separator, when ``section`` is empty).
    - Markers absent or malformed: append ``section`` after a blank-line
      separator (the blank line keeps a leading ``---`` from turning the
      author's last line into a setext heading). Empty ``section`` is a no-op.

    Feeding this function's own output back in is idempotent.
    """
    body = body or ""
    start_idx = body.find(start)
    end_idx = body.find(end)

    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        region_end = end_idx + len(end)
        before = body[:start_idx]
        after = body[region_end:]
        if section:
            return before + section + after
        # Removing: also drop the blank separator we inserted before the block
        # so repeated add/remove cycles don't accumulate blank lines.
        return before.rstrip("\n") + after

    if not section:
        return body
    if not body.strip():
        return section
    return body.rstrip() + "\n\n" + section


CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]


def fetch_codeowners(repo: str) -> tuple[str, str] | None:
    """Fetch CODEOWNERS from the repo's default branch via the Contents API.

    Checks the three locations GitHub supports, in priority order:
    ``.github/CODEOWNERS``, ``CODEOWNERS``, ``docs/CODEOWNERS``.

    Returns a ``(text, path)`` tuple identifying which location was used (so the
    caller can build a link to it), or ``None`` if no CODEOWNERS file exists.
    """
    for path in CODEOWNERS_PATHS:
        result = gh(
            ["api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            return base64.b64decode(result.stdout.strip()).decode(), path
        except Exception:
            continue
    return None


def yesno_regex(keyword: str) -> re.Pattern[str]:
    """Match the current template format and capture ``yes`` or ``no``.

    Examples that match (state captured):
      - **Urgent** (needs same-day review): yes
      - **High complexity** (non-obvious logic, careful review): no
      * **urgent**: YES

    The bullet must appear at the start of a line so that an inline ``*``
    from markdown bold syntax (e.g. ``**Urgent**`` inside a legacy checkbox
    line ``- [x] **Urgent**: no further action``) cannot be mistaken for a
    list bullet -- otherwise ``: no`` from the description would be captured
    and flip a checked legacy box from ``on`` to ``off``.
    """
    return re.compile(
        rf"^\s*[-*]\s*[*_`]*\s*{re.escape(keyword)}\b[^:\n]*:\s*(yes|no)\b",
        re.IGNORECASE | re.MULTILINE,
    )


def checkbox_regex(keyword: str) -> re.Pattern[str]:
    """Match the legacy template format and capture the checkbox state.

    Examples that match (state captured):
      - [x] **Urgent**: needs same-day review
      - [ ] **High complexity**: ...
      * [X] urgent
    """
    return re.compile(
        rf"[-*]\s*\[\s*([xX ])\s*\]\s*[*_`]*\s*{re.escape(keyword)}",
        re.IGNORECASE,
    )


URGENT_YESNO_REGEX = yesno_regex(URGENT_KEYWORD)
COMPLEXITY_YESNO_REGEX = yesno_regex(COMPLEXITY_KEYWORD)
URGENT_CHECKBOX_REGEX = checkbox_regex(URGENT_KEYWORD)
COMPLEXITY_CHECKBOX_REGEX = checkbox_regex(COMPLEXITY_KEYWORD)


@dataclass
class LabelPlan:
    """Planned label changes for a single PR."""

    pr_number: int
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        for label in self.add:
            parts.append(f"+{label}")
        for label in self.remove:
            parts.append(f"-{label}")
        parts.extend(self.notes)
        return (
            f"PR #{self.pr_number} " + " ".join(parts)
            if parts
            else f"PR #{self.pr_number} (no changes)"
        )


# Bounded retry policy for transient GitHub API failures (e.g. HTTP 504). gh()
# retries only failures classified transient by _is_retryable_gh_error; real
# errors (404, auth, bad input) fail fast.
GH_MAX_ATTEMPTS = 3
GH_BACKOFF_BASE_SECONDS = 1.0

# Case-insensitive substrings that mark a gh failure as a momentary
# server/network hiccup rather than a real error.
_RETRYABLE_GH_SIGNATURES = (
    "http 502",
    "http 503",
    "http 504",
    "couldn't respond to your request in time",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "early eof",
    "unexpected eof",
    "secondary rate limit",
    "abuse detection",
)


def _is_retryable_gh_error(stderr: str) -> bool:
    """Classify a gh failure as transient (worth retrying) from its stderr.

    Matches the signatures GitHub emits for momentary server or network hiccups
    (5xx, request timeouts, dropped connections, secondary rate limits). Real
    errors (404, auth, bad input) carry none of these and must fail fast.
    """
    haystack = stderr.lower()
    return any(sig in haystack for sig in _RETRYABLE_GH_SIGNATURES)


def gh(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subprocess, retrying transient GitHub API failures.

    Retries only failures classified transient by ``_is_retryable_gh_error``, up
    to ``GH_MAX_ATTEMPTS`` with exponential backoff plus jitter. A non-retryable
    failure honors ``check`` immediately (raising ``CalledProcessError`` when
    ``check`` is true, else returning the completed process), so a 404 or auth
    error still fails fast. The final attempt likewise honors ``check``.
    """
    for attempt in range(1, GH_MAX_ATTEMPTS + 1):
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            return result
        final_attempt = attempt == GH_MAX_ATTEMPTS
        if final_attempt or not _is_retryable_gh_error(result.stderr):
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr
                )
            return result
        delay = GH_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1) + random.uniform(
            0, GH_BACKOFF_BASE_SECONDS / 2
        )
        print(
            f"gh transient error (attempt {attempt}/{GH_MAX_ATTEMPTS}), "
            f"retrying in {delay:.1f}s: {result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        time.sleep(delay)
    # The loop returns or raises on the final attempt; this is unreachable.
    raise AssertionError("gh() retry loop exited without returning")


def fetch_pr(repo: str, pr_number: int) -> dict:
    result = gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "number,additions,deletions,body,labels,state,files,headRefOid",
        ]
    )
    return json.loads(result.stdout)


# Paths only, and paged. The REST equivalent (repos/{repo}/pulls/{n}/files)
# embeds the full patch hunk for every file with no way to project it away:
# measured against a 351-file dependency bump it transfers ~1.8MB to learn 351
# strings, where this query costs ~25KB. `--paginate` drives it off pageInfo,
# so `$endCursor` and the pageInfo selection are load-bearing for gh, not
# decoration. GitHub stops the connection at 3000 files, the same ceiling the
# REST endpoint and the PR "Files changed" tab enforce, so beyond that no API
# reports the tail and domain labels are best-effort.
PR_FILES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
}
"""


def fetch_pr_file_paths(repo: str, pr_number: int) -> list[str]:
    """Return every changed path for a PR, paging past gh's 100-file window."""
    owner, name = repo.split("/", 1)
    result = gh(
        [
            "api",
            "graphql",
            "--paginate",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
            "-f",
            f"query={PR_FILES_QUERY}",
            "--jq",
            ".data.repository.pullRequest.files.nodes[].path",
        ]
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def pr_changed_files(repo: str, pr_number: int, pr: dict) -> list[str]:
    """Return the PR's changed paths, re-fetching when they may be truncated.

    Prefers the list already embedded in the ``fetch_pr`` payload and only
    spends a second API call when that list is long enough to be suspect -- see
    GH_PR_VIEW_FILE_CAP for why a truncated list silently breaks domain
    labeling rather than merely degrading it.
    """
    files = [f["path"] for f in pr.get("files", [])]
    if len(files) < GH_PR_VIEW_FILE_CAP:
        return files
    files = fetch_pr_file_paths(repo, pr_number)
    print(f"PR #{pr_number} (paged {len(files)} changed files past gh's cap)")
    return files


def fetch_pr_inputs(repo: str, pr_number: int) -> dict:
    """Re-read only the fields a labeling decision is derived from."""
    result = gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "body,headRefOid"]
    )
    return json.loads(result.stdout)


def is_superseded(repo: str, pr_number: int, snapshot: dict) -> bool:
    """Report whether the PR's inputs moved since ``snapshot`` was taken.

    Runs that overlap on one PR have to converge on the newest view of it. The
    caller workflow used to get that from ``cancel-in-progress``, which kills
    the older run and leaves a cancelled check run on the head commit; Renovate
    reads every check run without discarding superseded ones
    (renovatebot/renovate#36837), sees a red branch status, and silently
    declines to automerge. Standing down here gives the same last-writer-wins
    ordering while the run still finishes green.

    Only the inputs are compared: the body, which decides the risk and template
    labels, and the head sha, which decides size and domain. Writing labels or
    the sticky comment changes neither, so a run whose inputs held still always
    proceeds -- the newest run cannot be starved by the ones it superseded.
    """
    current = fetch_pr_inputs(repo, pr_number)
    body_moved = (current.get("body") or "") != (snapshot.get("body") or "")
    head_moved = current.get("headRefOid") != snapshot.get("headRefOid")
    return body_moved or head_moved


def fetch_pr_comments(repo: str, pr_number: int) -> list[dict]:
    """Return the PR's issue comments as ``[{"id", "body"}, ...]``.

    ``--jq '.[] | {id, body}'`` streams one compact JSON object per line (NDJSON)
    across all paginated pages; ``id`` is the numeric REST id used to edit or
    delete the comment.
    """
    result = gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--jq",
            ".[] | {id, body}",
        ]
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def list_open_prs(repo: str) -> list[int]:
    result = gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number",
        ]
    )
    return [pr["number"] for pr in json.loads(result.stdout)]


def size_bucket(total: int) -> str | None:
    if total <= 0:
        return None
    if total <= 10:
        return "size/XS"
    if total <= 50:
        return "size/S"
    if total <= 250:
        return "size/M"
    if total <= 999:
        return "size/L"
    return "size/XL"


def risk_from_body(body: str, plan: LabelPlan) -> str | None:
    if CURSOR_SUMMARY_MARKER not in body:
        return None
    after = body.split(CURSOR_SUMMARY_MARKER, 1)[1]
    match = RISK_REGEX.search(after)
    if not match:
        plan.notes.append(
            "[warn: CURSOR_SUMMARY present but risk regex did not match -- check Bugbot format]"
        )
        return None
    level = match.group(1).lower()
    label = RISK_MAP.get(level)
    if label is None:
        plan.notes.append(
            f"[warn: unmapped Bugbot risk '{match.group(1)}' -> {RISK_FALLBACK}]"
        )
        return RISK_FALLBACK
    return label


def field_state(
    body: str,
    *,
    yesno: re.Pattern[str],
    checkbox: re.Pattern[str],
) -> str | None:
    """Return ``'on'``, ``'off'``, or ``None`` for a template field.

    Tries the current ``**Field**: yes/no`` syntax first and falls back to the
    legacy ``- [x] **Field**`` syntax. The legacy regex is retained so PRs
    opened before the template change keep being labeled correctly until the
    queue rolls over (~2 weeks). It will be removed in a follow-up.

    Defense in depth: skip any yes/no match whose enclosing line is itself a
    legacy checkbox line. The yes/no regex is anchored to the start of a
    line, so this shouldn't happen today, but a stray ``: no`` in a
    checkbox description must never preempt the checkbox result and flip a
    checked ``[x]`` from ``on`` to ``off``.
    """
    for match in yesno.finditer(body):
        line_start = body.rfind("\n", 0, match.start()) + 1
        newline = body.find("\n", match.end())
        line = body[line_start : newline if newline != -1 else len(body)]
        if checkbox.search(line):
            continue
        return "on" if match.group(1).lower() == "yes" else "off"
    match = checkbox.search(body)
    if match:
        return "on" if match.group(1).lower() == "x" else "off"
    return None


def reconcile(
    pr: dict,
    *,
    plan: LabelPlan,
    domain_slugs: set[str] | None = None,
) -> None:
    current_labels = {label["name"] for label in pr.get("labels", [])}
    body = pr.get("body") or ""
    additions = pr.get("additions", 0) or 0
    deletions = pr.get("deletions", 0) or 0

    # Size: pick exactly one bucket, remove any other size labels.
    desired_size = size_bucket(additions + deletions)
    for label in SIZE_LABELS:
        if label == desired_size:
            if label not in current_labels:
                plan.add.append(label)
        elif label in current_labels:
            plan.remove.append(label)

    # Risk: pick one (if any), remove other risk labels.
    desired_risk = risk_from_body(body, plan)
    for label in RISK_LABELS:
        if label == desired_risk:
            if label not in current_labels:
                plan.add.append(label)
        elif label in current_labels and desired_risk is not None:
            # Only remove an existing risk label when we have a new one; don't
            # strip a manually-set risk label just because Bugbot didn't comment.
            plan.remove.append(label)

    # Template fields: three-state (on/off/absent).
    for yesno_re, checkbox_re, label in [
        (URGENT_YESNO_REGEX, URGENT_CHECKBOX_REGEX, URGENT_LABEL),
        (COMPLEXITY_YESNO_REGEX, COMPLEXITY_CHECKBOX_REGEX, COMPLEXITY_LABEL),
    ]:
        state = field_state(body, yesno=yesno_re, checkbox=checkbox_re)
        if state == "on" and label not in current_labels:
            plan.add.append(label)
        elif state == "off" and label in current_labels:
            plan.remove.append(label)

    # Domain labels: add for matched teams, remove stale ones.
    if domain_slugs is not None:
        desired_domain = {
            f"{DOMAIN_LABEL_PREFIX}{s}" for s in domain_slugs if s in KNOWN_DOMAIN_SLUGS
        }
        for slug in KNOWN_DOMAIN_SLUGS:
            label = f"{DOMAIN_LABEL_PREFIX}{slug}"
            if label in desired_domain:
                if label not in current_labels:
                    plan.add.append(label)
            elif label in current_labels:
                plan.remove.append(label)


def apply(repo: str, plan: LabelPlan, dry_run: bool) -> None:
    if dry_run or (not plan.add and not plan.remove):
        return
    args = ["pr", "edit", str(plan.pr_number), "--repo", repo]
    for label in plan.add:
        args.extend(["--add-label", label])
    for label in plan.remove:
        args.extend(["--remove-label", label])
    gh(args)


def apply_body(repo: str, pr_number: int, new_body: str, dry_run: bool) -> None:
    """Set the PR body via ``gh pr edit`` (no-op on dry runs).

    The caller is responsible for only invoking this when the body actually
    changed, so we don't issue a redundant edit (and don't emit a needless
    ``edited`` timeline event).
    """
    if dry_run:
        return
    gh(["pr", "edit", str(pr_number), "--repo", repo, "--body", new_body])


def _normalize(text: str) -> str:
    """Normalize line endings and trim, so equality checks don't churn."""
    return text.replace("\r\n", "\n").strip()


def find_reasons_comments(
    comments: list[dict], marker: str = DOMAIN_REASONS_START
) -> list[dict]:
    """Return every sticky comment carrying ``marker``, lowest id first.

    More than one can exist: two labeler runs overlapping on the same PR can
    both observe "no comment yet" and both create one. Ordering by id gives
    every run the same answer for which copy is the original, so they can agree
    on a survivor without coordinating.
    """
    matches = [c for c in comments if marker in (c.get("body") or "")]
    return sorted(matches, key=lambda c: c["id"])


def find_reasons_comment(
    comments: list[dict], marker: str = DOMAIN_REASONS_START
) -> dict | None:
    """Return the surviving sticky comment, or ``None`` when there is none."""
    matches = find_reasons_comments(comments, marker)
    return matches[0] if matches else None


def plan_comment_action(section: str, existing: dict | None) -> tuple[str, str] | None:
    """Decide what to do with the sticky comment; return ``(action, note)``.

    Returns ``None`` when nothing needs to change. Actions are ``create`` (no
    comment yet, reasons to show), ``update`` (content changed), and ``delete``
    (a comment exists but there are no longer any reasons, e.g. the PR now
    touches only unowned files).
    """
    if section:
        if existing is None:
            return ("create", "[comment: domain reasons created]")
        if _normalize(existing.get("body") or "") != _normalize(section):
            return ("update", "[comment: domain reasons updated]")
        return None
    if existing is not None:
        return ("delete", "[comment: domain reasons removed]")
    return None


def apply_comment(
    repo: str,
    pr_number: int,
    action: str,
    section: str,
    existing_id: int | None,
    dry_run: bool,
) -> None:
    """Create, update, or delete the sticky reason comment (no-op on dry runs)."""
    if dry_run:
        return
    if action == "create":
        gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{pr_number}/comments",
                "-f",
                f"body={section}",
            ]
        )
    elif action == "update":
        gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{existing_id}",
                "-f",
                f"body={section}",
            ]
        )
    elif action == "delete":
        gh(
            [
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/issues/comments/{existing_id}",
            ]
        )


def prune_duplicate_reasons_comments(
    repo: str,
    pr_number: int,
    comments: list[dict],
    dry_run: bool,
    marker: str = DOMAIN_REASONS_START,
) -> int:
    """Delete every sticky comment but the lowest-id one; return how many.

    Overlapping runs can each create a copy, and only the survivor is ever
    updated afterwards, so an unpruned duplicate would sit on the PR forever.
    Because the survivor is chosen by lowest id rather than by who got here
    first, two runs pruning at once pick the same one and converge instead of
    deleting each other's. ``check`` is off so losing that race -- the other run
    already deleted the copy, giving a 404 -- is not treated as a failure.
    """
    duplicates = find_reasons_comments(comments, marker)[1:]
    if not dry_run:
        for comment in duplicates:
            gh(
                [
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{repo}/issues/comments/{comment['id']}",
                ],
                check=False,
            )
    return len(duplicates)


def determine_targets(repo: str, pr_number_input: str, event_pr: str) -> list[int]:
    if pr_number_input == "all":
        return list_open_prs(repo)
    if pr_number_input:
        return [int(pr_number_input)]
    if event_pr:
        return [int(event_pr)]
    return []


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number_input = os.environ.get("PR_NUMBER", "").strip()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    event_pr = os.environ.get("EVENT_PR_NUMBER", "").strip()

    reasons_target = (
        os.environ.get("REASONS_TARGET", REASONS_TARGET_COMMENT).strip().lower()
    )
    if reasons_target not in (REASONS_TARGET_BODY, REASONS_TARGET_COMMENT):
        print(
            f"Unknown REASONS_TARGET={reasons_target!r}; "
            f"defaulting to {REASONS_TARGET_COMMENT!r}",
            file=sys.stderr,
        )
        reasons_target = REASONS_TARGET_COMMENT

    targets = determine_targets(repo, pr_number_input, event_pr)
    if not targets:
        print("No PR to process; exiting.")
        return 0

    print(
        f"Processing {len(targets)} PR(s) in {repo} "
        f"(dry_run={dry_run}, reasons_target={reasons_target})"
    )

    # Fetch CODEOWNERS once per run (same for all PRs in this repo).
    codeowners = fetch_codeowners(repo)
    codeowners_rules: list[CodeownersRule] | None = None
    codeowners_url: str | None = None
    if codeowners is not None:
        codeowners_text, codeowners_path = codeowners
        codeowners_rules = parse_codeowners(codeowners_text)
        codeowners_url = f"https://github.com/{repo}/blob/HEAD/{codeowners_path}"
        print(f"Loaded {len(codeowners_rules)} CODEOWNERS rule(s) for domain labeling")
    else:
        print("No CODEOWNERS found; skipping domain labeling")

    failures = 0
    for pr_number in targets:
        plan = LabelPlan(pr_number=pr_number)
        try:
            pr = fetch_pr(repo, pr_number)
            if pr.get("state") != "OPEN":
                print(f"PR #{pr_number} (skip: not open)")
                continue

            # Match changed files against CODEOWNERS once; the label set is just
            # the owning slugs, and the reasons carry the files/patterns for the
            # body section. domain_slugs stays None (not an empty set) when there
            # is no CODEOWNERS so reconcile leaves existing domain labels alone;
            # note the body section below is still reconciled in that case, so a
            # stale block is cleaned up even after CODEOWNERS is removed.
            domain_slugs: set[str] | None = None
            reasons: dict[str, list[tuple[str, str]]] = {}
            if codeowners_rules is not None:
                files = pr_changed_files(repo, pr_number, pr)
                reasons = domain_reasons(codeowners_rules, files)
                domain_slugs = set(reasons)

            reconcile(pr, plan=plan, domain_slugs=domain_slugs)

            # A dry run writes nothing, so it has nothing to stand down from.
            if not dry_run and is_superseded(repo, pr_number, pr):
                print(f"PR #{pr_number} (skip: superseded by a newer run)")
                continue

            apply(repo, plan, dry_run)

            # Publish the domain-reason section (independent of labels so it and
            # label changes don't clobber each other). The body variant gets a
            # leading rule to separate it from the author's text; a standalone
            # comment has nothing above it, so it omits the rule.
            section = render_domain_reasons(
                reasons,
                codeowners_url,
                leading_rule=(reasons_target == REASONS_TARGET_BODY),
            )
            if reasons_target == REASONS_TARGET_COMMENT:
                comments = fetch_pr_comments(repo, pr_number)
                pruned = prune_duplicate_reasons_comments(
                    repo, pr_number, comments, dry_run
                )
                existing = find_reasons_comment(comments)
                action = plan_comment_action(section, existing)
                if action is not None:
                    verb, note = action
                    plan.notes.append(note)
                    existing_id = existing["id"] if existing else None
                    apply_comment(repo, pr_number, verb, section, existing_id, dry_run)
                    if verb == "create" and not dry_run:
                        # A run overlapping ours may have created its own copy
                        # after the fetch above, so settle it against fresh
                        # data. A dry run created nothing to settle.
                        pruned += prune_duplicate_reasons_comments(
                            repo,
                            pr_number,
                            fetch_pr_comments(repo, pr_number),
                            dry_run,
                        )
                if pruned:
                    plan.notes.append(f"[comment: pruned {pruned} duplicate(s)]")
            else:
                old_body = pr.get("body") or ""
                new_body = upsert_managed_section(
                    old_body, section, DOMAIN_REASONS_START, DOMAIN_REASONS_END
                )
                if new_body != old_body:
                    plan.notes.append("[body: domain reasons updated]")
                    apply_body(repo, pr_number, new_body, dry_run)

            print(plan.summary())
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(
                f"PR #{pr_number} (error: {exc.stderr.strip() or exc})",
                file=sys.stderr,
            )

    if (
        targets
        and failures >= MIN_FAILURES_TO_FAIL
        and failures / len(targets) > FAILURE_RATE_THRESHOLD
    ):
        print(
            f"Failure rate {failures}/{len(targets)} exceeds "
            f"{FAILURE_RATE_THRESHOLD:.0%} threshold; failing run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
