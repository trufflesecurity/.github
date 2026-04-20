#!/usr/bin/env python3
"""Sync labels from labels.yml to a single repo using `gh label create --force`.

This is intentionally additive: labels in the repo that are not in labels.yml
are left alone. This avoids deleting legacy labels that teams may still rely on
during the rollout.

Inputs (environment variables):
  GITHUB_REPOSITORY  e.g. "owner/repo" (always set on Actions)
  LABELS_FILE        path to labels.yml (defaults to ./labels.yml)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True)


def upsert_label(repo: str, label: dict) -> None:
    name = label["name"]
    color = label["color"]
    description = label.get("description", "")
    gh(
        [
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            color,
            "--description",
            description,
            "--force",
        ]
    )


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    labels_path = Path(os.environ.get("LABELS_FILE", "labels.yml"))
    labels = yaml.safe_load(labels_path.read_text())
    if not isinstance(labels, list):
        print(
            f"Expected a YAML list in {labels_path}, got {type(labels).__name__}",
            file=sys.stderr,
        )
        return 1

    print(f"Syncing {len(labels)} labels to {repo} from {labels_path}")
    failures = 0
    for label in labels:
        try:
            upsert_label(repo, label)
            print(f"  ok: {label['name']}")
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(
                f"  fail: {label.get('name', '<unknown>')}: {exc.stderr.strip() or exc}",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
