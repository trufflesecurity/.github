#!/usr/bin/env python3
"""PR labeler: compute size, risk, template-field, and domain labels for PRs.

Inputs come from environment variables set by the calling workflow:
  GITHUB_REPOSITORY  e.g. "owner/repo" (always set on Actions)
  PR_NUMBER          "" (event mode), "all" (backfill), or "<number>"
  DRY_RUN            "true" or "false"
  EVENT_PR_NUMBER    PR number from `pull_request` event (if any), else ""

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

For backfill mode (PR_NUMBER == "all"), per-PR failures are logged but do not
abort the run, unless more than 10% of PRs fail.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
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


def domains_for_pr(rules: list[CodeownersRule], changed_files: list[str]) -> set[str]:
    """Return the set of domain slugs that own any changed file."""
    teams: set[str] = set()
    for filepath in changed_files:
        matched_slugs: list[str] = []
        for pattern, slugs in rules:
            if _codeowners_match(pattern, filepath):
                matched_slugs = slugs
        teams.update(matched_slugs)
    return teams


CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]


def fetch_codeowners(repo: str) -> str | None:
    """Fetch CODEOWNERS from the repo's default branch via the Contents API.

    Checks the three locations GitHub supports, in priority order:
    ``.github/CODEOWNERS``, ``CODEOWNERS``, ``docs/CODEOWNERS``.
    """
    for path in CODEOWNERS_PATHS:
        result = gh(
            ["api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            return base64.b64decode(result.stdout.strip()).decode()
        except Exception:
            continue
    return None


def fetch_pr_files(repo: str, pr_number: int) -> list[str]:
    """Return the list of changed file paths for a PR."""
    result = gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "files"],
    )
    data = json.loads(result.stdout)
    return [f["path"] for f in data.get("files", [])]


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


def gh(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def fetch_pr(repo: str, pr_number: int) -> dict:
    result = gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "number,additions,deletions,body,labels,state",
        ]
    )
    return json.loads(result.stdout)


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

    targets = determine_targets(repo, pr_number_input, event_pr)
    if not targets:
        print("No PR to process; exiting.")
        return 0

    print(f"Processing {len(targets)} PR(s) in {repo} (dry_run={dry_run})")

    # Fetch CODEOWNERS once per run (same for all PRs in this repo).
    codeowners_text = fetch_codeowners(repo)
    codeowners_rules: list[CodeownersRule] | None = None
    if codeowners_text is not None:
        codeowners_rules = parse_codeowners(codeowners_text)
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

            domain_slugs: set[str] | None = None
            if codeowners_rules is not None:
                files = fetch_pr_files(repo, pr_number)
                domain_slugs = domains_for_pr(codeowners_rules, files)

            reconcile(pr, plan=plan, domain_slugs=domain_slugs)
            apply(repo, plan, dry_run)
            print(plan.summary())
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(
                f"PR #{pr_number} (error: {exc.stderr.strip() or exc})",
                file=sys.stderr,
            )

    if targets and failures / len(targets) > 0.10:
        print(
            f"Failure rate {failures}/{len(targets)} exceeds 10% threshold; failing run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
