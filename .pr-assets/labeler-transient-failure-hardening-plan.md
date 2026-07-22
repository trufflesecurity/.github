---
name: labeler transient failure hardening
overview: Harden the shared PR labeler against transient GitHub API errors (e.g. HTTP 504) so a momentary hiccup no longer fails an otherwise-healthy PR check, while still failing fast on real errors and catching systemic breakage during backfill.
todos:
  - id: retry
    content: Add module-level `import time`/`import random`, `GH_MAX_ATTEMPTS`/`GH_BACKOFF_BASE_SECONDS` constants, `_is_retryable_gh_error` helper, and a bounded retry-with-backoff loop in `gh()` in .github/scripts/pr_labeler.py; retry only transient failures, honor `check` otherwise.
    status: pending
  - id: threshold
    content: In `main()`, add `MIN_FAILURES_TO_FAIL = 2` and `FAILURE_RATE_THRESHOLD = 0.10`, and require the floor before the percentage gate applies.
    status: pending
  - id: tests
    content: Add pytest cases in .github/scripts/test_pr_labeler.py for retryable classification, gh() retry-then-success, non-retryable fast-fail, exhausted retries, and the threshold floor (monkeypatch subprocess.run/time.sleep and the fetch helpers).
    status: pending
  - id: verify
    content: Run `python -m pytest .github/scripts -v` and `ruff check`/`ruff format --check .github/scripts`; confirm green. Optional DRY_RUN=true smoke run.
    status: pending
isProject: false
---

# Harden the PR labeler against transient GitHub API failures

## Problem

A `pull_request` run of the reusable PR Labels workflow failed on a transient `HTTP 504`. Two weaknesses combined in [.github/scripts/pr_labeler.py](.github/scripts/pr_labeler.py):

- **No retry.** `gh()` (lines 435-436) runs the subprocess once; a 504 raises `CalledProcessError` immediately.
- **Zero-tolerance gate in event mode.** `main()` fails when `failures / len(targets) > 0.10` (lines 823-829). With one target, a single failure is `1/1 = 100%` and fails the run.

The 10% gate was designed for backfill (`PR_NUMBER="all"`, many PRs), not the single-target event path.

## Change 1 (primary): classified retry in `gh()`

In [.github/scripts/pr_labeler.py](.github/scripts/pr_labeler.py), near the API helpers:

- Add module-level `import time` and `import random` (currently absent; `import time` at module scope is required so tests can monkeypatch `pr_labeler.time.sleep`). Stdlib only, no new deps.
- Add named constants: `GH_MAX_ATTEMPTS = 3`, `GH_BACKOFF_BASE_SECONDS = 1.0`.
- Add a factored, unit-testable helper `_is_retryable_gh_error(stderr: str) -> bool` matching transient signatures case-insensitively: HTTP `502/503/504`, "couldn't respond to your request in time", "timeout"/"timed out", "connection reset"/"connection refused"/"EOF", and rate-limit/"abuse detection".
- Rewrite `gh()` as a bounded retry loop: run with `check=False` internally to inspect `stderr`/`returncode`; on a retryable non-zero result back off (`GH_BACKOFF_BASE_SECONDS * 2**(attempt-1)` plus small jitter) and retry; on a non-retryable result honor the caller's `check`; on the final attempt stop retrying and honor `check`.
- Emit one `stderr` line per retry for observability (attempt count, delay, trimmed stderr). No secrets (`gh` redacts the token).

This alone would have prevented the observed failure.

## Change 2 (safety net): floor the failure-rate gate

In `main()`:

- Add `MIN_FAILURES_TO_FAIL = 2` and `FAILURE_RATE_THRESHOLD = 0.10` (promote the literal `0.10`).
- Gate becomes `targets and failures >= MIN_FAILURES_TO_FAIL and failures / len(targets) > FAILURE_RATE_THRESHOLD`.
- Effect: a single failure never fails the run (event mode tolerates one exhausted-retry blip); backfill still fails at 2+ failures over 10%. Keep the log message reporting `failures`, `len(targets)`, and the threshold.

## Change 3: tests

In [.github/scripts/test_pr_labeler.py](.github/scripts/test_pr_labeler.py), mirroring the class-per-area layout and `monkeypatch` style:

- `_is_retryable_gh_error`: `True` for 504 / "couldn't respond ... in time" / timeout / connection reset; `False` for 404 / "not found" / empty stderr.
- `gh()` retry-then-success: monkeypatch `pr_labeler.subprocess.run` to return retryable failures N times then success; patch `pr_labeler.time.sleep` to a no-op; assert call count and returned stdout.
- `gh()` non-retryable: 404-style failure raises `CalledProcessError` after one attempt, no sleeps.
- `gh()` exhausted retries: retryable every attempt raises after `GH_MAX_ATTEMPTS`, slept `GH_MAX_ATTEMPTS - 1` times.
- Threshold floor: with one target and one failure `main()` returns `0`; with enough failures to clear both floor and rate, returns `1`. Drive by monkeypatching `determine_targets`, `fetch_codeowners`, and `fetch_pr` rather than hitting the network.

## Verification

- `python -m pytest .github/scripts -v` (matches CI in [.github/workflows/test-scripts.yml](.github/workflows/test-scripts.yml)).
- `ruff check .github/scripts` and `ruff format --check .github/scripts` (matches [.github/workflows/lint.yml](.github/workflows/lint.yml)).
- Optional manual dry run: set `GH_TOKEN`, `GITHUB_REPOSITORY`, `EVENT_PR_NUMBER`, `DRY_RUN=true`, `REASONS_TARGET=comment`, then `python3 .github/scripts/pr_labeler.py`.

## Rollout

Callers reference the reusable workflow ([.github/workflows/pr-labeler-reusable.yml](.github/workflows/pr-labeler-reusable.yml)) at `@main`, so merging to `main` propagates to all repos with no per-repo change. Re-running the originally failed job is a separate manual remediation, not part of this change.

## Out of scope / alternatives

- Retry-only: leaves zero tolerance if retries exhaust. The floor is cheap insurance.
- Floor-only: stops this one case but leaves every API call one blip from a per-PR error and no backfill protection.
- Relying on `gh`'s built-in retry: explicit classified retries keep the transient-vs-real distinction testable and in code.

## PR workflow

### Human note (verbatim - do not edit)

> I've run into this transient failure a couple of times, so I decided to go ahead and harden this to transient network effects.

### Execution parameters

- Implementation state: pre-implementation (clean tree, on `main`, 0 commits ahead) - B2 (worktree) and B3 (implementation) run.
- Branch: `martinlocklear/cursor/labeler-transient-failure-hardening`
- Worktree: `/tmp/github-labeler-transient-failure-hardening`
- pr-assets plan doc: yes, `.pr-assets/labeler-transient-failure-hardening-plan.md` on `martinlocklear/cursor/pr-assets`
- Quality gates: `python -m pytest .github/scripts -v`; `ruff check .github/scripts`; `ruff format --check .github/scripts` (no Makefile; gates mirror CI)
- Commit strategy: single commit (small, cohesive change to one script + its tests)
- Team checklist (A3a): this repo has no CODEOWNERS-based team-checklist mechanism, so the check is expected to no-op; re-confirmed in Stage B.

### Linear ticket (draft - to create, assign to me, mark In Progress)

- Team: Integrations. Project: none (CI/dev-tooling; precedent [INT-765](https://linear.app/truffle-security/issue/INT-765) carries no project). Label: `Task`. Priority: Medium. Assignee: me. State on create: In Progress.
- Title: `Harden the shared PR labeler against transient GitHub API failures`
- Description:

  > Problem: A `pull_request` run of the reusable PR Labels workflow (`.github/scripts/pr_labeler.py`) failed on a transient `HTTP 504` from the GitHub API. Two weaknesses combined - `gh()` runs each API call once with no retry, and the failure-rate gate has zero tolerance in event mode (one target, so `1/1 = 100% > 10%`). A momentary blip fails an otherwise-healthy PR check.
  >
  > Fix: bounded classified retry with backoff in `gh()` (retry 502/503/504, timeouts, connection resets; re-raise real errors immediately), plus a `MIN_FAILURES_TO_FAIL` floor so a single failure never fails event mode while backfill still catches systemic breakage. Unit tests cover classification, retry-then-success, non-retryable fast-fail, exhausted retries, and the floor.
  >
  > Done when: transient GitHub API errors retry and succeed instead of failing the run; a single failure in event mode does not fail the check; real errors still fail fast; `python -m pytest .github/scripts -v` is green. Ships to all caller repos on merge to `main` (the reusable workflow is referenced at `@main`).
  >
  > Drafted by Cursor on behalf of @martinlocklear.

### PR description (draft - generated body below the human note)

```markdown
> **Human note - written by Martin, verbatim, not AI-generated:**
>
> I've run into this transient failure a couple of times, so I decided to go ahead and harden this to transient network effects.

---

[authored by Cursor]

## Summary

- Add classified retry with backoff to `gh()` so transient GitHub API errors (502/503/504, timeouts, connection resets) retry instead of failing the run.
- Floor the failure-rate gate with `MIN_FAILURES_TO_FAIL = 2` so a single blip never fails event-mode labeling, while backfill still catches systemic breakage.
- Cover both with unit tests: retry classification, retry-then-success, non-retryable fast-fail, exhausted retries, and the threshold floor.

## Motivation

A `pull_request` labeler run failed on a one-off `HTTP 504`. `gh()` ran each API call once, and the 10% failure gate has zero tolerance with a single target (`1/1 = 100%`), so a momentary hiccup failed an otherwise-healthy PR check. Retry is the real fix; the floor is the backstop. Merging to `main` propagates to every repo that calls the reusable labeler workflow at `@main`.

Tracked in [INT-802](https://linear.app/truffle-security/issue/INT-802).

**Background:** [plan doc](<PLAN_DOC_URL>)

## Test plan

- [x] `python -m pytest .github/scripts -v`
- [x] `ruff check .github/scripts` and `ruff format --check .github/scripts`
- [ ] CI checks green
```

### Conversation summary (draft)

```markdown
[authored by Cursor]

## Conversation summary

This PR came from investigating a PR-labeler run that failed on a transient GitHub `HTTP 504`.

**Plan doc:** [labeler transient failure hardening](<PLAN_DOC_URL>)

### Key decisions

- **Retry in the `gh()` wrapper, classified by stderr signature.** One chokepoint covers every API call; a factored `_is_retryable_gh_error` helper keeps the transient-vs-real distinction unit-testable and re-raises real errors (404, auth, bad input) immediately.
- **Floor the failure-rate gate rather than replace it.** `MIN_FAILURES_TO_FAIL = 2` stops event mode from failing on a single exhausted-retry blip while backfill still fails on 2+ failures over 10%.

### Alternatives considered

- **Retry only** - leaves event mode zero-tolerance if retries ever exhaust.
- **Floor only** - stops this specific case but leaves every API call one blip from a per-PR error, with no backfill protection.
- **Rely on `gh`'s built-in retry** - less explicit and not unit-testable; the classified wrapper keeps the logic in code.
```

### Remaining Stage B steps (on confirmation)

B1 push plan doc to pr-assets; B2 worktree; B3 implement per todos; B4 gates + advisory lint; B5 single commit; B6 reconcile drafts with the diff; B7 pr-overlap-check + link-verify + `gh pr create --draft --base main`; B8 post conversation summary comment; B9 babysit; B10 cleanup.