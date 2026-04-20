#!/usr/bin/env python3
"""PR labeler: compute size, risk, and checkbox labels for one or more PRs.

Inputs come from environment variables set by the calling workflow:
  GITHUB_REPOSITORY  e.g. "trufflesecurity/thog" (always set on Actions)
  PR_NUMBER          "" (event mode), "all" (backfill), or "<number>"
  DRY_RUN            "true" or "false"
  EVENT_PR_NUMBER    PR number from `pull_request` event (if any), else ""

The script processes each PR by:
  1. Fetching additions, deletions, body, and current labels from the GitHub API.
  2. Computing the size bucket from additions+deletions.
  3. Parsing the Bugbot CURSOR_SUMMARY block for a risk level.
  4. Parsing the PR template checkboxes for `urgent` and `high complexity`.
  5. Reconciling with current labels and applying adds/removes via `gh pr edit`.

For backfill mode (PR_NUMBER == "all"), per-PR failures are logged but do not
abort the run, unless more than 10% of PRs fail.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

SIZE_LABELS = ["size/XS", "size/S", "size/M", "size/L", "size/XL"]
RISK_LABELS = ["risk/low", "risk/medium", "risk/high"]
URGENT_LABEL = "review/urgent"
COMPLEXITY_LABEL = "complexity/high"

CURSOR_SUMMARY_MARKER = "<!-- CURSOR_SUMMARY -->"
RISK_REGEX = re.compile(r"\*\*(\w+)\s+Risk\*\*", re.IGNORECASE)
RISK_MAP = {
    "low": "risk/low",
    "medium": "risk/medium",
    "high": "risk/high",
}
# Conservative fallback for unmapped Bugbot levels (e.g., "Critical", "Minimal").
RISK_FALLBACK = "risk/high"


# Match a markdown checkbox followed (with whitespace and optional bold/markdown
# punctuation) by a target keyword. The `[xX ]` part captures the state.
def checkbox_regex(keyword: str) -> re.Pattern[str]:
    # Examples that should match (state captured):
    #   - [x] **Urgent**: needs same-day review
    #   - [ ] **High complexity**: ...
    #   * [X] urgent
    return re.compile(
        rf"[-*]\s*\[\s*([xX ])\s*\]\s*[*_`]*\s*{re.escape(keyword)}",
        re.IGNORECASE,
    )


URGENT_REGEX = checkbox_regex("urgent")
COMPLEXITY_REGEX = checkbox_regex("high complexity")


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


def checkbox_state(body: str, regex: re.Pattern[str]) -> str | None:
    """Return 'on', 'off', or None (not present)."""
    match = regex.search(body)
    if not match:
        return None
    return "on" if match.group(1).lower() == "x" else "off"


def reconcile(
    pr: dict,
    *,
    plan: LabelPlan,
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

    # Checkboxes: three-state (on/off/absent).
    for regex, label in [
        (URGENT_REGEX, URGENT_LABEL),
        (COMPLEXITY_REGEX, COMPLEXITY_LABEL),
    ]:
        state = checkbox_state(body, regex)
        if state == "on" and label not in current_labels:
            plan.add.append(label)
        elif state == "off" and label in current_labels:
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

    failures = 0
    for pr_number in targets:
        plan = LabelPlan(pr_number=pr_number)
        try:
            pr = fetch_pr(repo, pr_number)
            if pr.get("state") != "OPEN":
                print(f"PR #{pr_number} (skip: not open)")
                continue
            reconcile(pr, plan=plan)
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
